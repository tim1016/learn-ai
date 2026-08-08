"""Typed evidence and deterministic broker support for synthetic Clerk drills."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.fault_injection import FrameFaultKind, WriteFaultKind
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg, BrokerPosition
from app.services.account_custody_synthetic_scenarios import SyntheticScenarioId


class SyntheticScenarioStatus(StrEnum):
    PASSED = "PASSED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class FaultSeamLimitationCode(StrEnum):
    WRITE_TIMEOUT_PRE_SDK = "WRITE_TIMEOUT_PRE_SDK"
    FRAME_FAULT_REQUIRES_LIVE_CONSUMER = "FRAME_FAULT_REQUIRES_LIVE_CONSUMER"
    FAULT_SEAM_NOT_PERMITTED = "FAULT_SEAM_NOT_PERMITTED"


@dataclass(frozen=True)
class FaultSeamLimitation:
    code: FaultSeamLimitationCode
    fault_kind: str
    detail: str


@dataclass(frozen=True)
class SimulatedBrokerOrderProof:
    client_order_id: str
    broker_order_id: str
    side: str
    quantity: float | None
    status: str
    filled_quantity: float


@dataclass(frozen=True)
class SimulatedBrokerProof:
    orders: tuple[SimulatedBrokerOrderProof, ...]
    positions: tuple[tuple[str, float], ...]
    submit_calls: tuple[str, ...]
    cancel_calls: tuple[str, ...]
    lookup_calls: tuple[str, ...]


@dataclass(frozen=True)
class SyntheticEvidenceWorksheet:
    authority_generation: int
    db_identity_token: str
    revision_before: int
    revision_after: int
    command_id: str | None
    effect_operation_id: str | None
    command_ids: tuple[str, ...]
    effect_operation_ids: tuple[str, ...]
    order_ref: str | None
    broker_order_id: str | None
    order_refs: tuple[str, ...]
    broker_order_ids: tuple[str, ...]
    receipt_id: str | None
    source_event_at_ms: int | None
    clerk_observed_at_ms: int
    recorded_at_ms: int
    operation_state: str
    broker_state: str | None
    expected: str
    observed: str
    action: str
    durable_transition_ref: str
    broker_before: SimulatedBrokerProof
    broker_after: SimulatedBrokerProof
    log_refs: tuple[str, ...]


@dataclass(frozen=True)
class SyntheticScenarioObservation:
    scenario_id: SyntheticScenarioId
    status: SyntheticScenarioStatus
    assertion: str
    evidence: SyntheticEvidenceWorksheet
    limitations: tuple[FaultSeamLimitation, ...] = ()
    fault_seam_required: bool = False
    fault_seam_exercised: bool = False


@dataclass(frozen=True)
class SyntheticCustodyDrillReport:
    account_prefix: str
    live_scenario_executed: bool
    production_generation_opened_read_write: bool
    fault_injection_permitted: bool
    fault_seam_exercised: bool
    fault_registry_write_armed: tuple[str, ...]
    fault_registry_frame_armed: tuple[str, ...]
    observations: tuple[SyntheticScenarioObservation, ...]


LOST_RESPONSE_LIMITATION = FaultSeamLimitation(
    code=FaultSeamLimitationCode.WRITE_TIMEOUT_PRE_SDK,
    fault_kind=WriteFaultKind.TIMEOUT.value,
    detail=(
        "The existing paper-only timeout fault short-circuits before the Alpaca SDK. "
        "It cannot prove an order landed and then lost its HTTP response; the Clerk "
        "recovery path below therefore uses a simulated broker order and is PARTIAL."
    ),
)
REDELIVERY_LIMITATION = FaultSeamLimitation(
    code=FaultSeamLimitationCode.FRAME_FAULT_REQUIRES_LIVE_CONSUMER,
    fault_kind=FrameFaultKind.REDELIVER_LAST.value,
    detail=(
        "The existing redelivery fault is drained only inside the real trade_updates "
        "consumer wrapper. This drill proves command retry dedup but does not claim "
        "that a live websocket redelivery was injected."
    ),
)
PARTIAL_FILL_LIMITATION = FaultSeamLimitation(
    code=FaultSeamLimitationCode.FRAME_FAULT_REQUIRES_LIVE_CONSUMER,
    fault_kind=FrameFaultKind.PARTIAL_FILL.value,
    detail=(
        "The existing partial-fill fault is drained only inside the real "
        "trade_updates consumer. The custody race used observed simulated broker "
        "state and does not claim the registry fault was armed or consumed."
    ),
)


def seam_not_permitted(kind: str) -> FaultSeamLimitation:
    return FaultSeamLimitation(
        code=FaultSeamLimitationCode.FAULT_SEAM_NOT_PERMITTED,
        fault_kind=kind,
        detail=(
            "The existing seam was not permitted by the runtime's explicit flag "
            "and paper-posture gate. The fallback remains synthetic and is PARTIAL."
        ),
    )


class TickClock:
    def __init__(self, value: int = 1_786_200_000_000) -> None:
        self.value = value

    def __call__(self) -> int:
        self.value += 10
        return self.value


class SyntheticBroker:
    """Stateful BrokerTradePort/BrokerReadPort simulation, not a fault seam."""

    broker_id = "alpaca"

    def __init__(self, clock: TickClock) -> None:
        self.clock = clock
        self.orders: dict[str, BrokerOrder] = {}
        self.positions: list[BrokerPosition] = []
        self.submit_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.lookup_calls: list[str] = []
        self.submit_error: Exception | None = None
        self.cancel_error: Exception | None = None
        self.fill_during_cancel_quantity: float | None = None
        self.cancel_started: asyncio.Event | None = None
        self.cancel_release: asyncio.Event | None = None

    def proof(self) -> SimulatedBrokerProof:
        return SimulatedBrokerProof(
            orders=tuple(
                SimulatedBrokerOrderProof(
                    client_order_id=ref,
                    broker_order_id=order.order_id,
                    side=order.side,
                    quantity=order.quantity,
                    status=order.status,
                    filled_quantity=order.filled_quantity,
                )
                for ref, order in sorted(self.orders.items())
            ),
            positions=tuple(sorted((position.symbol, position.quantity) for position in self.positions)),
            submit_calls=tuple(self.submit_calls),
            cancel_calls=tuple(self.cancel_calls),
            lookup_calls=tuple(self.lookup_calls),
        )

    def seed_order(
        self,
        client_order_id: str,
        *,
        quantity: float = 1.0,
        side: str = "buy",
        status: str = "accepted",
        filled_quantity: float = 0.0,
        broker_order_id: str | None = None,
    ) -> BrokerOrder:
        now = self.clock()
        order = BrokerOrder(
            broker="alpaca",
            order_id=broker_order_id or f"sim-{len(self.orders) + 1}",
            client_order_id=client_order_id,
            symbol="SPY",
            asset_class="us_equity",
            side=side,
            order_type="market",
            time_in_force="day",
            quantity=quantity,
            filled_quantity=filled_quantity,
            limit_price=None,
            stop_price=None,
            filled_avg_price=100.0 if filled_quantity else None,
            status=status,
            submitted_at_ms=now,
            created_at_ms=now,
            updated_at_ms=now,
            filled_at_ms=now if status == "filled" else None,
            canceled_at_ms=now if status == "canceled" else None,
            expired_at_ms=None,
            events=[],
            observed_at_ms=now,
        )
        self.orders[client_order_id] = order
        return order

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self.submit_calls.append(client_order_id)
        if self.submit_error is not None:
            raise self.submit_error
        return self.seed_order(
            client_order_id,
            quantity=leg.quantity,
            side=leg.side.value,
        )

    async def cancel(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)
        if self.cancel_error is not None:
            raise self.cancel_error
        if self.cancel_started is not None:
            self.cancel_started.set()
        if self.cancel_release is not None:
            await self.cancel_release.wait()
        match = next((item for item in self.orders.values() if item.order_id == order_id), None)
        if match is None:
            return
        now = self.clock()
        filled = self.fill_during_cancel_quantity or match.filled_quantity
        self.orders[match.client_order_id or ""] = match.model_copy(
            update={
                "status": "canceled",
                "filled_quantity": filled,
                "filled_avg_price": 100.0 if filled else None,
                "updated_at_ms": now,
                "canceled_at_ms": now,
                "observed_at_ms": now,
            }
        )

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        self.lookup_calls.append(client_order_id)
        return self.orders.get(client_order_id)

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
    ) -> list[BrokerOrder]:
        del after_ms
        orders = list(self.orders.values())
        if status == "open":
            orders = [item for item in orders if item.status in {"accepted", "new", "partially_filled"}]
        elif status == "closed":
            orders = [item for item in orders if item.status not in {"accepted", "new", "partially_filled"}]
        return orders[:limit] if limit is not None else orders

    async def list_positions(self) -> list[BrokerPosition]:
        return list(self.positions)


def synthetic_leg(*, quantity: float = 1.0) -> BrokerOrderLeg:
    return BrokerOrderLeg(symbol="SPY", side="buy", quantity=quantity)


def new_repo(
    artifacts_root: Path,
    *,
    suffix: str,
    bot_ids: tuple[str, ...] = ("spy-bot",),
) -> tuple[ClerkSqliteRepository, TickClock, str, dict[str, str]]:
    clock = TickClock()
    account_id = f"QUAL1415-{suffix}-{uuid4().hex[:8]}"
    repo = ClerkSqliteRepository.initialize(
        account_id=account_id,
        artifacts_root=artifacts_root,
        clock=clock,
        lease_ttl_ms=300_000,
    )
    run_ids: dict[str, str] = {}
    for index, bot_id in enumerate(bot_ids, start=1):
        repo.register_strategy_instance(
            strategy_instance_id=bot_id,
            symbol="SPY",
            config_hash=f"synthetic-{index}",
        )
        run_id = f"run-{index}"
        submit_start_run(
            repo,
            account_id=account_id,
            strategy_instance_id=bot_id,
            lifecycle_run_id=run_id,
            clock=clock,
        )
        run_ids[bot_id] = run_id
    return repo, clock, account_id, run_ids


def worksheet(
    repo: ClerkSqliteRepository,
    *,
    revision_before: int,
    broker_before: SimulatedBrokerProof,
    broker_after: SimulatedBrokerProof,
    expected: str,
    observed: str,
    action: str,
    receipt_id: str | None = None,
    source_event_at_ms: int | None = None,
) -> SyntheticEvidenceWorksheet:
    transitions = repo.custody_transitions()
    transition = transitions[-1]
    meta = repo.control_meta_snapshot()
    durable_ref = f"transition:{transition['sequence']}:{transition['row_hash']}"
    db_log_ref = f"{repo.db_path}#custody_transitions/{transition['sequence']}"
    mirror_log_ref = f"{repo.mirror_path}#row_hash={transition['row_hash']}"
    order_refs = tuple(
        dict.fromkeys(
            item["order_ref"]
            for item in transitions
            if item["order_ref"] is not None
        )
    )
    command_ids = tuple(
        dict.fromkeys(
            item["command_id"]
            for item in transitions
            if item["command_id"] is not None
        )
    )
    effect_operation_ids = tuple(
        dict.fromkeys(
            item["effect_operation_id"]
            for item in transitions
            if item["effect_operation_id"] is not None
        )
    )
    broker_order_ids = tuple(
        dict.fromkeys(
            (
                *(
                    item["broker_order_id"]
                    for item in transitions
                    if item["broker_order_id"] is not None
                ),
                *(order.broker_order_id for order in broker_before.orders),
                *(order.broker_order_id for order in broker_after.orders),
            )
        )
    )
    return SyntheticEvidenceWorksheet(
        authority_generation=meta.authority_generation,
        db_identity_token=meta.db_identity_token,
        revision_before=revision_before,
        revision_after=meta.control_revision,
        command_id=transition["command_id"],
        effect_operation_id=transition["effect_operation_id"],
        command_ids=command_ids,
        effect_operation_ids=effect_operation_ids,
        order_ref=transition["order_ref"],
        broker_order_id=transition["broker_order_id"],
        order_refs=order_refs,
        broker_order_ids=broker_order_ids,
        receipt_id=receipt_id,
        source_event_at_ms=(
            source_event_at_ms
            if source_event_at_ms is not None
            else transition["source_event_at_ms"]
        ),
        clerk_observed_at_ms=transition["clerk_observed_at_ms"],
        recorded_at_ms=transition["recorded_at_ms"],
        operation_state=transition["operation_state"],
        broker_state=transition["broker_state"],
        expected=expected,
        observed=observed,
        action=action,
        durable_transition_ref=durable_ref,
        broker_before=broker_before,
        broker_after=broker_after,
        log_refs=(db_log_ref, mirror_log_ref),
    )
