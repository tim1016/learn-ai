"""A resting multi-leg venue read through Alpaca's adapter, with no broker connection (#2913)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.broker.alpaca import adapter
from app.broker.alpaca.clerk import option_effects
from app.broker.alpaca.clerk.models import EffectOperationReceipt, EffectPurpose
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite.budget_commands import submit_budgeted_deploy
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerPosition
from app.broker.contract.options import BrokerOptionOrder, MultiLegOptionOrderRequest, OptionOrderRequest
from app.config import settings
from app.services import market_liveness
from tests._helpers.market_quotes import publish_quotes
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _FakeTradePort, _TestClock, documented_option_listing
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import PaperAccountRead, PaperOptions, _compose
from tests.broker.alpaca.clerk.sqlite.several_legs_fixtures import STRADDLE, STRADDLE_PICKS, evidence
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS, _gate, _new_budget_repo
from tests.broker.alpaca.documented_examples import documented


class MultiLegVenue(_FakeTradePort):
    async def submit_option_order(self, order: OptionOrderRequest, *, client_order_id: str) -> BrokerOptionOrder:
        if not isinstance(order, MultiLegOptionOrderRequest):
            return await super().submit_option_order(order, client_order_id=client_order_id)
        self.option_submit_calls.append(client_order_id)
        self.submitted_option_orders.append(order)
        if self.option_submit_error is not None:
            raise self.option_submit_error
        payload = deepcopy(documented("option_orders", "documented_responses.json")["MultilegOptionsOrderResponse"])
        at = self._venue_stamp()
        templates = payload["legs"]
        payload.update(
            id=str(uuid5(NAMESPACE_URL, client_order_id)), client_order_id=client_order_id,
            qty=str(order.quantity), filled_qty="0", filled_avg_price=None, status="new", filled_at=None,
            type=order.order_type.value, order_type=order.order_type.value,
            limit_price=None if order.net_limit_price is None else str(order.net_limit_price),
            created_at=at, updated_at=at, submitted_at=at,
        )
        legs = []
        for n, leg in enumerate(order.legs):
            child = deepcopy(templates[n % len(templates)])
            child.update(
                id=str(uuid5(NAMESPACE_URL, f"venue:{client_order_id}:{n}")),
                client_order_id=f"venue:{client_order_id}:{n}", symbol=leg.contract.occ_symbol,
                side=leg.side.value, position_intent=f"{leg.side.value}_to_{leg.intent.value}",
                ratio_qty=str(leg.ratio), qty=str(order.quantity * leg.ratio), filled_qty="0",
                filled_avg_price=None, status="new", filled_at=None, created_at=at, updated_at=at, submitted_at=at,
            )
            legs.append(child)
        payload["legs"] = legs
        self.option_orders[client_order_id] = payload
        if self.option_answer_lost:
            raise BrokerUnavailable("the venue took the structure and its answer was lost", broker="alpaca")
        return adapter.from_alpaca_option_order(payload)

    def legs_fill(self, parent_ref: str, counts: tuple[int, ...], *, state: str = "partially_filled") -> None:
        payload = self.option_orders[parent_ref]
        at = self._venue_stamp()
        for leg, count in zip(payload["legs"], counts, strict=True):
            leg.update(
                status=state, filled_qty=str(count), filled_avg_price="3.14" if leg["side"] == "buy" else "1.20",
                updated_at=at, filled_at=at if count else None,
            )
        payload.update(status=state, filled_qty=str(min(counts)), filled_avg_price="6.28", updated_at=at)

    async def cancel(self, order_id: str) -> None:
        await super().cancel(order_id)
        for payload in self.option_orders.values():
            if payload["id"] == order_id and payload["status"] == "canceled":
                for leg in payload["legs"]:
                    leg.update(status="canceled", canceled_at=payload["canceled_at"], updated_at=payload["updated_at"])


class MultiLegAccount(PaperAccountRead):
    async def list_positions(self) -> list[BrokerPosition]:
        held: dict[str, float] = {}
        for parent in self._venue.option_orders.values():
            for leg in parent["legs"]:
                held[leg["symbol"]] = held.get(leg["symbol"], 0) + float(leg["filled_qty"]) * (
                    1 if leg["side"] == "buy" else -1
                )
        return [
            BrokerPosition(
                broker="alpaca", symbol=symbol, asset_id=None, asset_class="us_option", quantity=abs(count),
                side="long" if count > 0 else "short", average_entry_price=3.14, market_value=314 * count,
                cost_basis=314 * count, current_price=3.14, unrealized_pl=0, unrealized_plpc=0, observed_at_ms=NOON,
            )
            for symbol, count in held.items() if count
        ]


@dataclass
class SeveralLegsPaper(PaperOptions):
    venue: MultiLegVenue
    account: MultiLegAccount

    async def restart(self) -> None:
        """Close the Clerk and recover its durable books, keeping only the venue's own state."""
        await self.keeper.stop()
        await self.sync.stop()
        await self.clerk.drain_effects()
        account_id = self.repo.account_id
        self.repo.close()
        self.repo = ClerkSqliteRepository.open(account_id=account_id, artifacts_root=self.artifacts_root, clock=self.clock)
        self.sync, self.keeper, self.clerk = _compose(self.repo, self.clock, self.venue, self.account)

    async def enter(self, sid: str = "a", *, units: int = 2, decision: str | None = None, **_unused: Any) -> EffectOperationReceipt:
        self.decisions += 1
        return await self.clerk.execute_for_instance(
            strategy_instance_id=sid, run_id=f"run-{sid}", decision_id=decision or f"enter-{self.decisions}",
            purpose=EffectPurpose.ENTER, action_plan=STRADDLE, quantity=units,
            option_entry=evidence(self.clock(), STRADDLE_PICKS),
        )


@pytest.fixture
async def multi_paper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SeveralLegsPaper]:
    monkeypatch.setattr(option_effects, "BOUGHT_LEG_STRUCTURES_TAKEN", True)
    monkeypatch.setattr(settings, "ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED", True)
    market_liveness.reset_market_liveness_store_for_testing()
    publish_quotes(NOON, SPY=585.0)
    repo = _new_budget_repo(tmp_path)
    for sid, cents in (("a", 200_000), ("b", 50_000)):
        submit_budgeted_deploy(
            repo, strategy_instance_id=sid, lifecycle_run_id=f"run-{sid}", world="real_paper",
            committed_cents=cents, configuration_hash=f"seal-{sid}",
            exit_terms_hash=canonical_sha256(TERMS.model_dump(mode="json")), risk_revision=1, actor="owner",
            envelope=_gate(cash=3000), minimum_position_cost=Decimal("100.01"),
        )
    clock = repo.clock
    assert isinstance(clock, _TestClock)
    venue = MultiLegVenue()
    venue.venue_clock = clock
    account = MultiLegAccount(venue)
    account.listed = {contract.occ_symbol: documented_option_listing(contract) for contract, *_quotes in STRADDLE_PICKS.values()}
    sync, keeper, clerk = _compose(repo, clock, venue, account)
    assert await sync.tick() == "observed"
    clock.advance(1_000)
    world = SeveralLegsPaper(
        repo=repo, clock=clock, clerk=clerk, venue=venue, account=account, keeper=keeper, sync=sync, artifacts_root=tmp_path,
    )
    try:
        yield world
    finally:
        await world.keeper.stop()
        await world.sync.stop()
        await world.clerk.drain_effects()
        world.repo.close()
        market_liveness.reset_market_liveness_store_for_testing()
