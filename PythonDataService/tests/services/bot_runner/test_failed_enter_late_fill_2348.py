"""#2348: a fill on an ENTER the Clerk already folded ``failed`` must alarm.

Before the fix the Clerk attributed the late fill to the ``failed`` ENTER, the
sweep read ``clean``, no episode was raised, and the strategy -- flat since the
refusal (path E) or since its EXIT was answered flat (paths C/D) -- only ever
emitted ENTER, which admission refused with ``ATTRIBUTED_EXPOSURE_EXISTS``
forever. Nothing ever exited the real position.

Real code: ``BotTaskRegistry`` -> ``run_trade_bot`` -> the sealed
``deployment_validation`` program -> ``SqliteAlpacaClerkFacade`` (real SQLite
repository, ENTER/EXIT/resolver/reconcile folds) -> the real
``SqliteTradeUpdateEvidenceSink``. Faked: the Alpaca broker port, the bar feed,
and the 30 s submit-absence grace.

After the late fill every path must end with the Clerk holding the real
position, an instance-scoped ``FAILED_ENTER_FILLED`` episode naming the order,
the bot's custody proof frozen on that fence (the account verdict stays
``clean``: broker and journal agree), ENTER refused, and a reducing EXIT (a
strategy EXIT on the active run) admitted -- whose fill then resolves the
episode and lifts the freeze.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import app.broker.alpaca.clerk.sqlite.order_evidence as order_evidence_module
from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.uncertainty import admit_new_exposure
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    FAILED_ENTER_FILLED_REASON_CODE,
)
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.errors import BrokerRequestInvalid, BrokerUnavailable
from app.broker.contract.models import (
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
    BrokerPosition,
)
from app.utils.timestamps import now_ms_utc
from tests._helpers.bot_runner.custody import _SID, _registry
from tests._helpers.bot_runner.doubles import _FakeFeed, _SqliteRuntimeBroker
from tests._helpers.bot_runner.market import patch_fresh_live_market_liveness
from tests._helpers.canary_admission import admit_canary_pairing
from tests.services.bot_runner._support import (
    _WIN_START_MS,
    _green_bar,
    _red_bar,
    _wait_for,
)


class _QueueFeed(_FakeFeed):
    def __init__(self) -> None:
        super().__init__([], mode="hold")
        self.q: asyncio.Queue = asyncio.Queue()
        # Bars the strategy has finished with: the consumer only resumes this
        # generator to ask for the next bar once the previous one -- clerk
        # call included -- is fully handled.
        self.bars_done = 0

    async def stream_bars(self, symbol, *, use_rth=True, continuity=None):
        self.continuity_seen = continuity
        while True:
            bar = await self.q.get()
            self.bars_consumed += 1
            yield bar
            self.bars_done += 1


class _LateLandingBroker(_SqliteRuntimeBroker):
    """The first BUY submit fails; ``land()`` later makes that order appear filled.

    ``land()`` is what #2342's abandoned alpaca-py worker (or #2304's
    duplicate-id retry) does: the exact ``client_order_id`` the Clerk folded
    ``failed`` turns out to be live at Alpaca and fills.
    """

    def __init__(self, first_error: Exception) -> None:
        super().__init__()
        self.first_error = first_error
        self.submit_log: list[tuple[str, str, float]] = []
        self.failed_coid: str | None = None
        self.positions: list[BrokerPosition] = []

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str):
        self.submit_log.append((leg.side, client_order_id, leg.quantity))
        if leg.side == "buy" and self.failed_coid is None:
            self.failed_coid = client_order_id
            raise self.first_error
        return await super().submit(leg, client_order_id=client_order_id)

    async def list_positions(self) -> list:
        return list(self.positions)

    def _filled(self, *, coid: str, order_id: str, side: str, qty: float) -> BrokerOrder:
        now = now_ms_utc()
        return BrokerOrder(
            broker="alpaca",
            order_id=order_id,
            client_order_id=coid,
            symbol="SPY",
            asset_class="us_equity",
            side=side,
            order_type="market",
            time_in_force="day",
            quantity=qty,
            filled_quantity=qty,
            limit_price=None,
            stop_price=None,
            filled_avg_price=400.0,
            status="filled",
            submitted_at_ms=now,
            created_at_ms=now,
            updated_at_ms=now,
            filled_at_ms=now,
            canceled_at_ms=None,
            expired_at_ms=None,
            events=[],
            observed_at_ms=now,
        )

    def _set_position(self, qty: float) -> None:
        now = now_ms_utc()
        self.positions = (
            []
            if qty == 0
            else [
                BrokerPosition(
                    broker="alpaca",
                    symbol="SPY",
                    asset_id=None,
                    asset_class="us_equity",
                    quantity=qty,
                    side="long",
                    average_entry_price=400.0,
                    market_value=400.0 * qty,
                    cost_basis=400.0 * qty,
                    current_price=400.0,
                    unrealized_pl=0.0,
                    unrealized_plpc=0.0,
                    observed_at_ms=now,
                )
            ]
        )

    def land(self) -> BrokerOrder:
        assert self.failed_coid is not None
        order = self._filled(
            coid=self.failed_coid, order_id="broker-late-1", side="buy", qty=1.0
        )
        self.orders[self.failed_coid] = order
        self._set_position(1.0)
        return order

    def fill_working_sells(self) -> None:
        for coid, order in list(self.orders.items()):
            if order.side == "sell" and order.status == "accepted":
                self.orders[coid] = self._filled(
                    coid=coid, order_id=order.order_id, side="sell", qty=order.quantity
                )
                self._set_position(0.0)


def _instance_episodes(repo: ClerkSqliteRepository) -> list[dict]:
    return [
        uncertainty
        for uncertainty in repo.active_uncertainties()
        if uncertainty["reason_code"] == FAILED_ENTER_FILLED_REASON_CODE
        and uncertainty["strategy_instance_id"] == _SID
    ]


async def _deliver_trade_updates(
    repo: ClerkSqliteRepository,
    clerk: SqliteAlpacaClerkFacade,
    order: BrokerOrder,
) -> None:
    sink = SqliteTradeUpdateEvidenceSink(repo=repo, intake=clerk.intake, reconciler=clerk)
    for event_type, qty, execution_id in (
        ("new", None, None),
        ("fill", 1.0, "exec-late-1"),
    ):
        await sink.record_lifecycle_event(
            client_order_id=order.client_order_id,
            event=BrokerOrderEvent(
                event_type=event_type,
                occurred_at_ms=order.updated_at_ms,
                price=400.0 if qty else None,
                quantity=qty,
                execution_id=execution_id,
            ),
            event_key=f"broker-late-1|{event_type}|{order.updated_at_ms}",
            order=order,
            recovery_source=None,
            recovery_window_limit=None,
        )


async def _run_late_fill_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    first_error: Exception,
    land: str,
    stream: bool,
) -> None:
    patch_fresh_live_market_liveness(monkeypatch)
    admit_canary_pairing(monkeypatch, "deployment_validation", "PA-TEST")
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    broker = _LateLandingBroker(first_error)
    clerk = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker, account_mode="paper")
    calls: list[tuple[str, str]] = []
    explanations: list[str] = []
    strategy_kwargs: list[dict] = []
    real_execute = clerk.execute_for_instance

    async def recording_execute(**kwargs):
        receipt = await real_execute(**kwargs)
        calls.append((kwargs["purpose"].value, receipt.state.value))
        explanations.append(receipt.explanation)
        strategy_kwargs.append(kwargs)
        return receipt

    clerk.execute_for_instance = recording_execute  # type: ignore[method-assign]
    feed = _QueueFeed()
    registry = _registry(tmp_path / "runner", feed, start_custody_guard=clerk.start_admission_snapshot)
    set_alpaca_clerk(clerk)
    base = _WIN_START_MS + 60_000
    deployed = False
    try:
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade")
        deployed = True
        for bar in (_green_bar(base), _green_bar(base + 60_000)):
            feed.q.put_nowait(bar)
        await _wait_for(lambda: len(calls) == 1, timeout_s=5)
        # The 30 s submit-absence grace has elapsed.
        monkeypatch.setattr(order_evidence_module, "submit_absence_grace_ms", lambda: 0)
        await clerk.reconcile_account(trigger="AUTOMATIC")

        async def land_late_order() -> None:
            order = broker.land()
            if stream:
                await _deliver_trade_updates(repo, clerk, order)
                # The trade_updates route alarms on its own, before any sweep.
                assert len(_instance_episodes(repo)) == 1
            await clerk.reconcile_account(trigger="AUTOMATIC")

        if land == "before_exit":
            await land_late_order()
        for bar in (_red_bar(base + 120_000), _red_bar(base + 180_000), _green_bar(base + 240_000)):
            feed.q.put_nowait(bar)
        await _wait_for(lambda: feed.bars_done == 5, timeout_s=5)
        if land == "after_exit":
            await land_late_order()
        for bar in (_green_bar(base + 300_000), _green_bar(base + 360_000), _green_bar(base + 420_000)):
            feed.q.put_nowait(bar)
        await _wait_for(lambda: feed.bars_done == 8, timeout_s=5)

        # The strategy, flat in its own eyes, kept deciding ENTER on the green
        # bars after the fence. Each came back a blocked receipt naming the
        # fence -- never an admission error the supervisor records as a crash
        # -- and the run kept consuming bars: after the operator's flatten
        # clears the fence it trades again without a restart.
        assert calls[-2:] == [("ENTER", "rejected"), ("ENTER", "rejected")], calls
        assert all(
            text.startswith(f"{FAILED_ENTER_FILLED_REASON_CODE}:") for text in explanations[-2:]
        ), explanations

        # The Clerk's belief still matches the broker: it holds the real long.
        assert repo.position(_SID, "SPY") == pytest.approx(1.0, abs=1e-9, rel=0)
        entry_ref = broker.failed_coid
        entry = repo.order(entry_ref)
        assert entry is not None
        assert repo.effect_operation(entry.effect_operation_id).state == "failed"

        # ...but it is no longer absorbed silently.
        episodes = _instance_episodes(repo)
        assert len(episodes) == 1, repo.active_uncertainties()
        episode = episodes[0]
        assert episode["scope"] == "CUSTODY_SUBJECT"
        assert episode["blocks_new_exposure"]
        assert episode["allows_reduction"]
        cause = json.loads(episode["facts_json"])["cause_facts"]
        assert cause["orders"] == [{"order_ref": entry_ref, "symbol": "SPY", "filled_qty": 1.0}]
        # Broker and journal agree, so the account verdict is clean; the fence
        # freezes this bot's custody proof instead, with its exposure known.
        verdict = (await clerk.reconcile_account(trigger="AUTOMATIC")).verdict
        assert verdict == "clean"
        proof = await clerk.prove_instance_custody(_SID)
        assert proof.freeze.active
        assert entry_ref in (proof.freeze.explanation or "")
        assert proof.exposure == {"SPY": pytest.approx(1.0, abs=1e-9, rel=0)}
        decision = admit_new_exposure(repo, strategy_instance_id=_SID)
        assert not decision.allowed
        assert decision.reason_code == FAILED_ENTER_FILLED_REASON_CODE

        # A strategy EXIT on the active run must be admitted: the fence
        # refuses exposure, not reduction.
        strategy_call = strategy_kwargs[-1]
        receipt = await real_execute(
            strategy_instance_id=_SID,
            run_id=strategy_call["run_id"],
            decision_id="strategy-exit:2348",
            purpose=EffectPurpose.EXIT,
            action_plan=strategy_call["action_plan"],
            quantity=strategy_call["quantity"],
        )
        assert receipt.state not in {
            EffectOperationState.REJECTED,
            EffectOperationState.UNPROVABLE,
            EffectOperationState.FLAT,
        }, receipt
        assert [side for side, _, _ in broker.submit_log if side == "sell"] == ["sell"]

        # The reduction fills; the next sweep proves the instance flat and
        # resolves the episode.
        broker.fill_working_sells()
        result = await clerk.reconcile_account(trigger="AUTOMATIC")
        assert repo.position(_SID, "SPY") == pytest.approx(0.0, abs=1e-9, rel=0)
        assert _instance_episodes(repo) == []
        assert result.verdict == "clean"
        assert not (await clerk.prove_instance_custody(_SID)).freeze.active
        assert admit_new_exposure(repo, strategy_instance_id=_SID).reason_code != (
            FAILED_ENTER_FILLED_REASON_CODE
        )
    finally:
        if deployed:
            await registry.stop("alpaca", _SID)
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_late_fill_after_exit_answered_flat_raises_episode_via_trade_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path C (#2342): voided ENTER lands and fills after the EXIT was answered flat."""
    await _run_late_fill_case(
        tmp_path,
        monkeypatch,
        first_error=BrokerUnavailable("SDK call exceeded fail_after(15 s)"),
        land="after_exit",
        stream=True,
    )


@pytest.mark.asyncio
async def test_late_fill_after_exit_answered_flat_raises_episode_via_rest_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path D: as C, but the trade_updates frames are missed; only the sweep sees it."""
    await _run_late_fill_case(
        tmp_path,
        monkeypatch,
        first_error=BrokerUnavailable("SDK call exceeded fail_after(15 s)"),
        land="after_exit",
        stream=False,
    )


@pytest.mark.asyncio
async def test_fill_on_rejected_duplicate_id_enter_raises_episode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path E (#2304): a duplicate-id reply folds the ENTER failed; the order fills."""
    await _run_late_fill_case(
        tmp_path,
        monkeypatch,
        first_error=BrokerRequestInvalid("client_order_id must be unique (422 after SDK 504 retry)"),
        land="before_exit",
        stream=True,
    )
