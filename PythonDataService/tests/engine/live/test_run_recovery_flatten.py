"""Unit tests for ``_recovery_flatten`` in ``app.engine.live.run``.

The cmd_start integration (exception → recovery flatten → exit 3) is
covered end-to-end in the FakeBroker shutdown integration test; these
tests verify the recovery helper in isolation.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.broker.ibkr.models import (
    IbkrOrderSpec,
    IbkrPosition,
    IbkrPositionsSnapshot,
)
from app.engine.live.account_clerk import AccountClerk
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    bot_order_namespace_for_instance,
    write_account_instance_binding,
)
from app.engine.live.live_state_sidecar import LiveStateEnvelope, LiveStateSidecarRepo
from app.engine.live.run import (
    _is_recovery_readonly,
    _journal_recovery_order_specs,
    _record_recovery_flatten_residual_incident,
    _recovery_flatten,
    _resolve_recovery_broker,
)
from app.operator.incidents.store import IncidentStore
from tests.engine.live.fixtures.fake_broker import FakeBroker


def _journal_intent(instance_id: str, run_id: str, intent_id: str, *, action: str) -> AccountOwnerSubmitIntent:
    namespace = bot_order_namespace_for_instance(instance_id)
    return AccountOwnerSubmitIntent(
        trace_id=f"trace-{intent_id}",
        account_id="DU123",
        strategy_instance_id=instance_id,
        run_id=run_id,
        bot_order_namespace=namespace,
        intent_id=intent_id,
        order_ref=f"{namespace}:{intent_id}",
        intent_kind="STRATEGY",
        order_spec=IbkrOrderSpec(
            symbol="SPY",
            sec_type="STK",
            action=action,
            quantity=1,
            order_type="MKT",
            time_in_force="DAY",
            confirm_paper=True,
            client_order_id=f"client-{intent_id}",
            order_ref=f"{namespace}:{intent_id}",
        ).model_dump(),
        owner_generation=1,
        created_at_ms=1,
    )


def _write_journal_binding(tmp_path, instance_id: str, run_id: str) -> None:
    write_account_instance_binding(
        tmp_path,
        AccountInstanceBinding(
            account_id="DU123",
            strategy_instance_id=instance_id,
            run_id=run_id,
            bot_order_namespace=bot_order_namespace_for_instance(instance_id),
            lifecycle_state="ACTIVE",
            recorded_at_ms=1,
            source="test",
        ),
    )


async def _record_journal_fill(
    clerk: AccountClerk,
    intent: AccountOwnerSubmitIntent,
    *,
    side: str,
    quantity: float,
    exec_id: str,
) -> None:
    from app.broker.ibkr.models import IbkrOrderEvent

    await clerk.record_intent(intent)
    await clerk.record_broker_event(
        IbkrOrderEvent(
            account_id="DU123",
            order_id=1,
            event_type="fill",
            order_ref=intent.order_ref,
            symbol="SPY",
            side=side,
            fill_quantity=quantity,
            exec_id=exec_id,
            ts_ms=1,
        )
    )


class _Args:
    def __init__(self, *, readonly: bool) -> None:
        self.readonly = readonly


class _ClientWithSettings:
    def __init__(self, *, readonly: bool) -> None:
        self.settings = type("_S", (), {"readonly": readonly})()


def _seed_position(broker: FakeBroker, symbol: str, quantity: float) -> None:
    broker.position_snapshot = IbkrPositionsSnapshot(
        account_id="DU123",
        is_paper=True,
        positions=[
            IbkrPosition(
                account_id="DU123",
                con_id=756733,
                symbol=symbol,
                sec_type="STK",
                quantity=quantity,
                avg_cost=500.0,
                fetched_at_ms=1,
            ),
        ],
        fetched_at_ms=1,
    )


@pytest.mark.asyncio
async def test_recovery_flatten_submits_one_sell_per_long_position() -> None:
    broker = FakeBroker()
    _seed_position(broker, "SPY", 100.0)

    liquidated = await _recovery_flatten(broker)

    assert liquidated == 1
    sell_orders = [o for o in broker.orders if o.action == "SELL"]
    assert len(sell_orders) == 1
    assert sell_orders[0].symbol == "SPY"
    assert sell_orders[0].quantity == 100


@pytest.mark.asyncio
async def test_recovery_flatten_routes_broker_writes_through_account_owner_writer() -> None:
    broker = FakeBroker()
    _seed_position(broker, "SPY", 100.0)
    boundaries: list[str] = []

    async def writer(*, boundary: str, write):
        boundaries.append(boundary)
        return await write()

    liquidated = await _recovery_flatten(
        broker,
        account_owner_broker_writer=writer,
    )

    assert liquidated == 1
    assert boundaries == ["broker.cancel_open_orders", "broker.place_order"]


@pytest.mark.asyncio
async def test_recovery_flatten_uses_clerk_for_the_halt_write_path(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bot may read positions, but its fenced adapter never writes recovery orders."""

    broker = FakeBroker()

    async def account_net_must_not_be_read():
        raise AssertionError("Clerk recovery must not size from account-net positions")

    broker.fetch_positions = account_net_must_not_be_read
    _seed_position(broker, "SPY", 100.0)
    submitted = []
    monkeypatch.setattr(
        "app.engine.live.run._journal_recovery_order_specs",
        lambda **_kwargs: (
            IbkrOrderSpec(
                symbol="SPY",
                sec_type="STK",
                action="SELL",
                quantity=1,
                order_type="MKT",
                time_in_force="DAY",
                confirm_paper=True,
                client_order_id="recovery-flat",
                order_ref="learn-ai/bot-a/v1:recovery-intent",
            ),
        ),
    )

    async def clerk_submitter(intents):
        submitted.extend(intents)
        return tuple(
            SimpleNamespace(
                broker_acked=SimpleNamespace(
                    order_id=701,
                    perm_id=702,
                    status="Submitted",
                    symbol="SPY",
                )
            )
            for _intent in intents
        )

    liquidated = await _recovery_flatten(
        broker,
        bot_order_namespace="learn-ai/bot-a/v1",
        account_clerk_recovery_submitter=clerk_submitter,
        recovery_account_id="DU123",
        recovery_strategy_instance_id="bot-a",
        recovery_run_id="run-a",
        recovery_owner_generation=42,
        recovery_artifacts_root=tmp_path,
    )

    assert liquidated == 1
    assert broker.orders == []
    [intent] = submitted
    assert intent.intent_kind == "RECOVERY_FLATTEN"
    assert intent.bot_order_namespace == "learn-ai/bot-a/v1"


@pytest.mark.asyncio
async def test_clerk_recovery_flatten_empty_journal_plan_returns_zero_without_rpc(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-flat Clerk namespace must not submit an invalid empty batch."""

    broker = FakeBroker()
    monkeypatch.setattr("app.engine.live.run._journal_recovery_order_specs", lambda **_kwargs: ())

    async def empty_batch_must_not_be_submitted(_intents: tuple[AccountOwnerSubmitIntent, ...]) -> object:
        raise AssertionError("empty Clerk recovery batch must not make an RPC call")

    liquidated = await _recovery_flatten(
        broker,
        bot_order_namespace="learn-ai/bot-a/v1",
        account_clerk_recovery_submitter=empty_batch_must_not_be_submitted,
        recovery_account_id="DU123",
        recovery_strategy_instance_id="bot-a",
        recovery_run_id="run-a",
        recovery_owner_generation=42,
        recovery_artifacts_root=tmp_path,
    )

    assert liquidated == 0


@pytest.mark.asyncio
async def test_clerk_recovery_batch_persists_each_acknowledgement_for_relaunch(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clerk-owned recovery keeps every order/permId ownership fingerprint."""

    broker = FakeBroker()
    sidecar_path = tmp_path / "live_state.json"
    seed = LiveStateEnvelope(
        strategy_instance_id="bot-a",
        run_id="run-a",
        bot_order_namespace="learn-ai/bot-a/v1",
        ib_client_id=42,
        last_processed_bar_ms=1,
        last_artifact_flush_ms=1,
    )
    monkeypatch.setattr(
        "app.engine.live.run._journal_recovery_order_specs",
        lambda **_kwargs: (
            IbkrOrderSpec(
                symbol="SPY",
                sec_type="STK",
                action="SELL",
                quantity=1,
                order_type="MKT",
                time_in_force="DAY",
                confirm_paper=True,
                client_order_id="recovery-spy",
                order_ref="learn-ai/bot-a/v1:recovery-spy",
            ),
            IbkrOrderSpec(
                symbol="QQQ",
                sec_type="STK",
                action="BUY",
                quantity=2,
                order_type="MKT",
                time_in_force="DAY",
                confirm_paper=True,
                client_order_id="recovery-qqq",
                order_ref="learn-ai/bot-a/v1:recovery-qqq",
            ),
        ),
    )

    async def clerk_submitter(intents):
        return tuple(
            SimpleNamespace(
                broker_acked=SimpleNamespace(
                    order_id=700 + index,
                    perm_id=800 + index,
                    status="PreSubmitted",
                )
            )
            for index, _intent in enumerate(intents)
        )

    liquidated = await _recovery_flatten(
        broker,
        bot_order_namespace="learn-ai/bot-a/v1",
        account_clerk_recovery_submitter=clerk_submitter,
        recovery_account_id="DU123",
        recovery_strategy_instance_id="bot-a",
        recovery_run_id="run-a",
        recovery_owner_generation=42,
        recovery_artifacts_root=tmp_path,
        live_state_path=sidecar_path,
        live_state_seed=seed,
    )

    saved = LiveStateSidecarRepo(sidecar_path).read()
    assert liquidated == 2
    assert saved is not None
    assert saved.known_perm_ids == [800, 801]
    assert saved.submitted_orders["recovery-spy"]["symbol"] == "SPY"
    assert saved.submitted_orders["recovery-qqq"]["symbol"] == "QQQ"


@pytest.mark.asyncio
async def test_journal_recovery_plan_uses_only_target_namespace_signed_exposure(tmp_path) -> None:
    """Recovery ignores sibling and foreign account flow, and preserves sign."""

    _write_journal_binding(tmp_path, "bot-a", "run-a")
    _write_journal_binding(tmp_path, "bot-b", "run-b")
    clerk = AccountClerk(artifacts_root=tmp_path, account_id="DU123")
    target_buy = _journal_intent("bot-a", "run-a", "target-buy", action="BUY")
    target_sell = _journal_intent("bot-a", "run-a", "target-sell", action="SELL")
    sibling = _journal_intent("bot-b", "run-b", "sibling", action="BUY")
    await _record_journal_fill(clerk, target_buy, side="BUY", quantity=3, exec_id="target-buy")
    await _record_journal_fill(clerk, target_sell, side="SELL", quantity=5, exec_id="target-sell")
    await _record_journal_fill(clerk, sibling, side="BUY", quantity=99, exec_id="sibling")

    # A manual/foreign fill remains account truth but has no namespace, so it
    # cannot change the target bot's recovery plan.
    from app.broker.ibkr.models import IbkrOrderEvent

    await clerk.record_broker_event(
        IbkrOrderEvent(
            account_id="DU123",
            order_id=4,
            event_type="fill",
            order_ref="manual-order",
            symbol="SPY",
            side="BUY",
            fill_quantity=500,
            exec_id="manual",
            ts_ms=1,
        )
    )

    [spec] = _journal_recovery_order_specs(
        artifacts_root=tmp_path,
        account_id="DU123",
        bot_order_namespace=bot_order_namespace_for_instance("bot-a"),
    )

    assert spec.symbol == "SPY"
    assert spec.action == "BUY"
    assert spec.quantity == 2


@pytest.mark.asyncio
async def test_record_recovery_flatten_residual_incident_persists_open_positions(
    tmp_path,
) -> None:
    broker = FakeBroker()
    _seed_position(broker, "SPY", 1.0)

    await _record_recovery_flatten_residual_incident(
        run_dir=tmp_path,
        broker=broker,
        occurred_at_ms=1_700_000_000_000,
        error_summary="TimeoutError()",
    )

    [incident] = IncidentStore(tmp_path).list_unresolved()
    assert incident.notice.code == "watchdog.flatten_failed"
    assert "SPY +1" in incident.notice.message
    assert incident.evidence["residual_positions"] == {"SPY": 1.0}


class FetchPositionsFailureBroker(FakeBroker):
    async def fetch_positions(self):
        raise TimeoutError("positions unavailable")


@pytest.mark.asyncio
async def test_record_recovery_flatten_residual_incident_persists_uncertain_fetch_failure(
    tmp_path,
) -> None:
    broker = FetchPositionsFailureBroker()

    await _record_recovery_flatten_residual_incident(
        run_dir=tmp_path,
        broker=broker,
        occurred_at_ms=1_700_000_000_000,
        error_summary="TimeoutError('positions unavailable')",
    )

    [incident] = IncidentStore(tmp_path).list_unresolved()
    assert incident.notice.code == "watchdog.flatten_failed"
    assert incident.evidence["positions_fetch_failed"] is True
    assert incident.evidence["residual_positions"] is None


class RejectingPlaceOrderBroker(FakeBroker):
    async def place_order(self, spec, *, perm_id_wait_s: float = 0.0):
        raise TimeoutError(f"order rejected for {spec.symbol}")


@pytest.mark.asyncio
async def test_recovery_flatten_reports_order_level_failures() -> None:
    broker = RejectingPlaceOrderBroker()
    _seed_position(broker, "SPY", 100.0)
    failed_symbols: list[str] = []

    liquidated = await _recovery_flatten(broker, failed_symbols=failed_symbols)

    assert liquidated == 0
    assert failed_symbols == ["SPY"]


class DeferredPermIdBroker(FakeBroker):
    """Models real IBKR permId timing.

    ``IB.placeOrder`` returns synchronously while the order is still
    ``PendingSubmit`` — IBKR has not assigned a ``permId`` yet; it arrives a
    beat later on the ``openOrder`` callback (together with the move out of
    ``PendingSubmit``). So a placement that does NOT wait sees ``perm_id is
    None``; only a caller that opts into the permId wait (``perm_id_wait_s >
    0``) gets the stable id. The recovery path must opt in, otherwise it
    records ``None`` and the next same-account relaunch cannot recognize the
    replayed recovery fill as its own.
    """

    REAL_PERM_ID = 1176469133

    async def place_order(self, spec, *, perm_id_wait_s: float = 0.0):
        ack = await super().place_order(spec)  # perm_id=None, PendingSubmit
        if perm_id_wait_s > 0:
            return ack.model_copy(
                update={"perm_id": self.REAL_PERM_ID, "status": "PreSubmitted"}
            )
        return ack


@pytest.mark.asyncio
async def test_recovery_flatten_records_real_perm_id_to_live_state_sidecar(tmp_path) -> None:
    """Recovery-flatten orders must be recognizable on same-account relaunch.

    Regression: the recovery path recorded the *synchronous* ack's permId —
    which IBKR has not assigned yet at PendingSubmit, so it persisted
    ``None``. IBKR then replayed the resulting execution on relaunch with a
    real permId and no client_order_id, and the outside-mutation guard had
    nothing to match it against, fatal-halting the bot on its own recovery
    fill until the session date rolled.

    The fix: the recovery path opts into the permId wait, so the durable trail
    captures the stable permId the replayed execution will carry.
    """
    sidecar_path = tmp_path / "live_state.json"
    repo = LiveStateSidecarRepo(sidecar_path)
    repo.write(
        LiveStateEnvelope(
            strategy_instance_id="spy_ema_crossover",
            run_id="run-fixture",
            bot_order_namespace="learn-ai/spy_ema_crossover/v1",
            ib_client_id=42,
            last_processed_bar_ms=1_780_000_000_000,
            last_artifact_flush_ms=1_780_000_000_500,
        )
    )
    broker = DeferredPermIdBroker()
    _seed_position(broker, "SPY", 100.0)

    liquidated = await _recovery_flatten(broker, live_state_path=sidecar_path)

    assert liquidated == 1
    loaded = repo.read()
    assert loaded is not None
    assert loaded.known_perm_ids == [DeferredPermIdBroker.REAL_PERM_ID]
    [client_order_id] = loaded.submitted_orders.keys()
    assert client_order_id.startswith("recovery-flatten-SPY-")
    assert (
        loaded.submitted_orders[client_order_id]["perm_id"]
        == DeferredPermIdBroker.REAL_PERM_ID
    )


@pytest.mark.asyncio
async def test_recovery_flatten_seeds_live_state_when_sidecar_missing(tmp_path) -> None:
    """A first-crash recovery flatten must not drop its ownership trail.

    Regression: when ``live_state.json`` did not exist yet, the recovery
    fingerprint helper returned early. A crash before the first per-bar
    sidecar flush then lost the recovery-flatten permId, so the next relaunch
    could not recognize its own recovery fill.
    """
    sidecar_path = tmp_path / "live_state.json"
    repo = LiveStateSidecarRepo(sidecar_path)
    seed = LiveStateEnvelope(
        strategy_instance_id="spy_ema_crossover",
        run_id="run-fixture",
        bot_order_namespace="learn-ai/spy_ema_crossover/v1",
        ib_client_id=42,
        last_processed_bar_ms=1_780_000_000_000,
        last_artifact_flush_ms=1_780_000_000_500,
    )
    broker = DeferredPermIdBroker()
    _seed_position(broker, "SPY", 100.0)

    liquidated = await _recovery_flatten(
        broker,
        live_state_path=sidecar_path,
        live_state_seed=seed,
    )

    assert liquidated == 1
    loaded = repo.read()
    assert loaded is not None
    assert loaded.strategy_instance_id == "spy_ema_crossover"
    assert loaded.known_perm_ids == [DeferredPermIdBroker.REAL_PERM_ID]
    [client_order_id] = loaded.submitted_orders.keys()
    assert client_order_id.startswith("recovery-flatten-SPY-")


@pytest.mark.asyncio
async def test_recovery_flatten_submits_one_buy_per_short_position() -> None:
    broker = FakeBroker()
    _seed_position(broker, "SPY", -50.0)

    liquidated = await _recovery_flatten(broker)

    assert liquidated == 1
    buy_orders = [o for o in broker.orders if o.action == "BUY"]
    assert len(buy_orders) == 1
    assert buy_orders[0].symbol == "SPY"
    assert buy_orders[0].quantity == 50


@pytest.mark.asyncio
async def test_recovery_flatten_no_positions_returns_zero() -> None:
    broker = FakeBroker()
    liquidated = await _recovery_flatten(broker)
    assert liquidated == 0
    assert broker.orders == []


@pytest.mark.asyncio
async def test_recovery_flatten_readonly_does_not_place_or_cancel_orders() -> None:
    """In readonly mode the recovery path must NOT touch the broker.

    Regression: a paper-week dry-run with ``--readonly`` crashed
    mid-session (duplicate IBKR 5-second bar timestamp) and
    ``_recovery_flatten`` placed a real SELL MKT against the paper
    account, breaking the documented ``IBKR_READONLY=true`` contract
    on ``IbkrSettings.readonly`` ("every call to place_paper_order
    raises OrderRefusedError before any contract is built"). On a
    flag-flip to live this would have closed a real position on an
    arbitrary engine crash.

    Readonly must enumerate positions for the operator log and return
    the detected count, but must not call ``place_order`` or
    ``cancel_open_orders``.
    """

    class RaisingCancelAndPlaceBroker(FakeBroker):
        async def cancel_open_orders(self) -> list[int]:
            raise AssertionError("cancel_open_orders must not be called in readonly mode")

        async def place_order(self, spec, *, perm_id_wait_s: float = 0.0):
            raise AssertionError("place_order must not be called in readonly mode")

    broker = RaisingCancelAndPlaceBroker()
    _seed_position(broker, "SPY", 100.0)

    detected = await _recovery_flatten(broker, readonly=True)

    assert detected == 1
    assert broker.orders == []


@pytest.mark.asyncio
async def test_recovery_flatten_cancel_failure_does_not_block_flatten() -> None:
    """A broker failure on cancel_open_orders must not skip the flatten path.

    The whole point of recovery flatten is to leave the account flat
    even when something has misbehaved. Swallowing the cancel failure
    and proceeding to liquidate matches that intent.
    """

    class RaisingCancelBroker(FakeBroker):
        async def cancel_open_orders(self) -> list[int]:
            raise RuntimeError("simulated broker timeout on cancel")

    broker = RaisingCancelBroker()
    _seed_position(broker, "SPY", 50.0)

    liquidated = await _recovery_flatten(broker)

    assert liquidated == 1
    sell_orders = [o for o in broker.orders if o.action == "SELL"]
    assert len(sell_orders) == 1


@pytest.mark.asyncio
async def test_recovery_flatten_per_position_place_order_failure_keeps_loop() -> None:
    """If place_order fails for symbol A, symbol B still gets an attempt."""

    class FailFirstThenSucceedBroker(FakeBroker):
        def __init__(self) -> None:
            super().__init__()
            self._calls = 0

        async def place_order(self, spec, *, perm_id_wait_s: float = 0.0):
            self._calls += 1
            if self._calls == 1:
                raise RuntimeError("simulated place_order failure on first call")
            return await super().place_order(spec, perm_id_wait_s=perm_id_wait_s)

    broker = FailFirstThenSucceedBroker()
    broker.position_snapshot = IbkrPositionsSnapshot(
        account_id="DU123",
        is_paper=True,
        positions=[
            IbkrPosition(
                account_id="DU123",
                con_id=1,
                symbol="SPY",
                sec_type="STK",
                quantity=100.0,
                avg_cost=500.0,
                fetched_at_ms=1,
            ),
            IbkrPosition(
                account_id="DU123",
                con_id=2,
                symbol="QQQ",
                sec_type="STK",
                quantity=50.0,
                avg_cost=400.0,
                fetched_at_ms=1,
            ),
        ],
        fetched_at_ms=1,
    )

    liquidated = await _recovery_flatten(broker)

    # First position's place_order raised; second succeeded.
    assert liquidated == 1


def test_is_recovery_readonly_args_true_client_false_returns_true() -> None:
    """CLI --readonly must win even when IBKR_READONLY env var is false.

    Regression (CodeRabbit on PR #237): ``IbkrClient()`` reads settings
    from env only, so ``client.settings.readonly`` does NOT reflect the
    CLI ``--readonly`` flag. The original guard checked only the client
    side, which means an operator who set ``--readonly`` on the command
    line with ``IBKR_READONLY=false`` in ``.env`` would still get a
    real recovery-flatten on engine crash — defeating the explicit CLI
    intent. The helper must OR the two signals.
    """
    assert _is_recovery_readonly(_Args(readonly=True), _ClientWithSettings(readonly=False)) is True


def test_is_recovery_readonly_args_false_client_true_returns_true() -> None:
    """Env-var readonly still applies when no CLI flag is set."""
    assert _is_recovery_readonly(_Args(readonly=False), _ClientWithSettings(readonly=True)) is True


def test_is_recovery_readonly_both_false_returns_false() -> None:
    assert _is_recovery_readonly(_Args(readonly=False), _ClientWithSettings(readonly=False)) is False


def test_is_recovery_readonly_both_true_returns_true() -> None:
    assert _is_recovery_readonly(_Args(readonly=True), _ClientWithSettings(readonly=True)) is True


def test_is_recovery_readonly_no_client_uses_args_only() -> None:
    """When no client is in scope (early test paths) args.readonly is authoritative."""
    assert _is_recovery_readonly(_Args(readonly=True), None) is True
    assert _is_recovery_readonly(_Args(readonly=False), None) is False


def test_is_recovery_readonly_client_without_settings_falls_back_to_args() -> None:
    """A malformed client missing .settings must not raise AttributeError."""

    class _ClientNoSettings:
        pass

    assert _is_recovery_readonly(_Args(readonly=True), _ClientNoSettings()) is True
    assert _is_recovery_readonly(_Args(readonly=False), _ClientNoSettings()) is False


def test_resolve_recovery_broker_prefers_injected_broker() -> None:
    fake = FakeBroker()
    resolved = _resolve_recovery_broker(fake, None)
    assert resolved is fake


def test_resolve_recovery_broker_returns_none_when_no_broker_no_client() -> None:
    assert _resolve_recovery_broker(None, None) is None


def test_resolve_recovery_broker_returns_none_when_client_disconnected() -> None:
    class _DisconnectedClient:
        def is_connected(self) -> bool:
            return False

    # A disconnected client should yield None regardless of broker presence
    # — recovery_flatten can't operate without a live broker session.
    assert _resolve_recovery_broker(None, _DisconnectedClient()) is None
    assert _resolve_recovery_broker(FakeBroker(), _DisconnectedClient()) is None


# ──────────────────────────── VCR-0019 ───────────────────────────────


class _StaleThenFreshBroker(FakeBroker):
    """Models the VCR-0019 race: the engine's prior strategy SELL filled at
    the broker between the first ``fetch_positions`` call and the post-cancel
    refresh. The initial snapshot says ``qty=1`` (stale); every subsequent
    refresh says ``qty=0`` (fresh / fill propagated). If recovery_flatten
    iterates the stale snapshot without refreshing, it submits a duplicate
    SELL and the paper account goes net-short."""

    def __init__(self, *, symbol: str, stale_qty: float) -> None:
        super().__init__()
        self._symbol = symbol
        self._stale_qty = stale_qty
        self._fetches = 0

    async def fetch_positions(self) -> IbkrPositionsSnapshot:
        self._fetches += 1
        qty = self._stale_qty if self._fetches == 1 else 0.0
        return IbkrPositionsSnapshot(
            account_id="DU123",
            is_paper=True,
            positions=[
                IbkrPosition(
                    account_id="DU123",
                    con_id=1,
                    symbol=self._symbol,
                    sec_type="STK",
                    quantity=qty,
                    avg_cost=500.0,
                    fetched_at_ms=1,
                ),
            ],
            fetched_at_ms=1,
        )


@pytest.mark.asyncio
async def test_recovery_flatten_does_not_duplicate_sell_when_broker_fill_propagates_post_snapshot_vcr_0019() -> None:
    """VCR-0019 — the recovery path must NOT submit a duplicate SELL when
    the strategy's prior exit fill landed at the broker between the initial
    ``fetch_positions`` call and the moment we'd otherwise place a
    liquidation order.

    Receipt: 2026-06-16 HITL run, intent_events.jsonl seq 24-27 — the engine
    saw ``position=1`` in its initial snapshot, cancelled open orders, then
    submitted a SECOND SELL that took the account net-short. Manual cleanup
    via /api/broker/orders was required (and emergency-flatten was also
    broken — see VCR-0020).
    """
    broker = _StaleThenFreshBroker(symbol="SPY", stale_qty=1.0)

    liquidated = await _recovery_flatten(broker)

    assert liquidated == 0, "no orders may be submitted when the broker reports flat"
    assert broker.orders == [], "duplicate SELL would short the account"


@pytest.mark.asyncio
async def test_recovery_flatten_stamps_order_ref_when_namespace_provided_vcr_0020() -> None:
    """VCR-0020 — recovery_flatten must stamp a deterministic ``order_ref``
    on each spec so a ``requires_durable_submit=True`` broker (real IBKR
    adapter) accepts the submission. The spec's ``order_ref`` must parse
    as ``{namespace}:{intent_id}`` with the engine's namespace prefix."""
    broker = FakeBroker()
    _seed_position(broker, "SPY", 100.0)

    liquidated = await _recovery_flatten(
        broker, bot_order_namespace="learn-ai/spy_ema_paper/v1"
    )

    assert liquidated == 1
    [spec] = broker.orders
    assert spec.order_ref is not None
    assert spec.order_ref.startswith("learn-ai/spy_ema_paper/v1:")
    # Final-colon split → 22-char base64url intent_id suffix.
    _ns, _, intent = spec.order_ref.rpartition(":")
    assert len(intent) == 22


@pytest.mark.asyncio
async def test_recovery_flatten_sources_namespace_from_live_state_seed() -> None:
    """When no explicit ``bot_order_namespace`` is passed, the seed envelope
    is consulted. cmd_start passes the explicit param today, but the seed
    is the fall-back path for older callers and post-flush envelopes."""
    sidecar_seed = LiveStateEnvelope(
        strategy_instance_id="seed_sid",
        run_id="run-seed",
        bot_order_namespace="learn-ai/seed_sid/v1",
        ib_client_id=7,
        last_processed_bar_ms=1,
        last_artifact_flush_ms=2,
    )
    broker = FakeBroker()
    _seed_position(broker, "QQQ", 50.0)

    liquidated = await _recovery_flatten(broker, live_state_seed=sidecar_seed)

    assert liquidated == 1
    [spec] = broker.orders
    assert spec.order_ref is not None
    assert spec.order_ref.startswith("learn-ai/seed_sid/v1:")


def test_resolve_recovery_broker_returns_engine_broker_to_preserve_owned_order_ids() -> None:
    """Recovery flatten must use the SAME broker instance the engine ran with.

    Reviewer feedback (P1.2): the prior implementation, when no test
    ``broker_arg`` was supplied, wrapped the client in a FRESH
    ``IbkrBrokerAdapter`` whose ``_owned_order_ids`` was empty. Calling
    ``cancel_open_orders`` on that fresh adapter is a no-op against
    the runner's actual in-flight orders, so recovery would submit
    liquidations while the original orders kept working — double-state
    on the account.

    The fix: cmd_start now constructs the broker explicitly and passes
    the same instance to both ``LiveEngine(broker=...)`` and the
    recovery flatten via ``_resolve_recovery_broker(broker, client)``.
    This test pins the helper's identity contract.
    """

    class _ConnectedClient:
        def is_connected(self) -> bool:
            return True

    engine_broker = FakeBroker()
    resolved = _resolve_recovery_broker(engine_broker, _ConnectedClient())
    assert resolved is engine_broker  # SAME instance, not a fresh adapter
