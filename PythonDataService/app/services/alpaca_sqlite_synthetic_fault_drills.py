"""Write-seam lost-response scenarios for the synthetic Clerk rehearsal."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from alpaca.common.exceptions import APIError

from app.broker.alpaca.broker import AlpacaBroker
from app.broker.alpaca.clerk.sqlite.enter import resolve_enter_submission, submit_enter
from app.broker.alpaca.clerk.sqlite.exit import accept_exit, resolve_exit
from app.broker.alpaca.client import AlpacaTradingClient
from app.broker.alpaca.fault_injection import (
    WriteFaultKind,
    craft_write_error,
    get_fault_injection_registry,
)
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
from app.broker.contract.ports import BrokerTradePort
from app.services.account_custody_synthetic_scenarios import SyntheticScenarioId
from app.services.alpaca_sqlite_synthetic_drill_support import (
    LOST_RESPONSE_LIMITATION,
    FaultSeamLimitation,
    SimulatedBrokerOrderProof,
    SimulatedBrokerProof,
    SyntheticBroker,
    SyntheticScenarioObservation,
    fault_seam_permitted,
    fault_seam_status,
    new_repo,
    seam_not_permitted,
    synthetic_leg,
    worksheet,
)
from app.services.alpaca_sqlite_synthetic_drill_support import (
    require_invariant as _require,
)


class _NoNetworkAlpacaSdkClient:
    """Deterministic SDK-shaped paper transport for the production client hook."""

    def __init__(self) -> None:
        self.post_calls: list[tuple[str, Any]] = []
        self.delete_calls: list[tuple[str, Any]] = []
        self.lookup_calls: list[str] = []
        self.orders: dict[str, dict[str, Any]] = {}

    def post(self, path: str, data: Any = None) -> dict[str, Any]:
        self.post_calls.append((path, data))
        request = dict(data or {})
        client_order_id = str(request["client_order_id"])
        order = {
            "id": f"synthetic-paper-{len(self.orders) + 1}",
            "client_order_id": client_order_id,
            "created_at": "2026-08-07T18:00:00Z",
            "updated_at": "2026-08-07T18:00:00Z",
            "submitted_at": "2026-08-07T18:00:00Z",
            "filled_at": None,
            "canceled_at": None,
            "expired_at": None,
            "symbol": request["symbol"],
            "asset_class": "us_equity",
            "qty": request["qty"],
            "filled_qty": "0",
            "filled_avg_price": None,
            "order_type": request["type"],
            "type": request["type"],
            "side": request["side"],
            "time_in_force": request["time_in_force"],
            "limit_price": request.get("limit_price"),
            "stop_price": None,
            "status": "accepted",
        }
        self.orders[client_order_id] = order
        return dict(order)

    def delete(self, path: str, data: Any = None) -> None:
        self.delete_calls.append((path, data))

    def get_order_by_client_id(self, client_id: str) -> dict[str, Any]:
        self.lookup_calls.append(client_id)
        order = self.orders.get(client_id)
        if order is not None:
            return dict(order)
        body = json.dumps({"code": 40410000, "message": "order not found"})
        response = SimpleNamespace(status_code=404, headers={})
        http_error = SimpleNamespace(response=response, request=None)
        raise APIError(body, http_error=http_error)

    def proof(self) -> SimulatedBrokerProof:
        return SimulatedBrokerProof(
            orders=tuple(
                SimulatedBrokerOrderProof(
                    client_order_id=client_order_id,
                    broker_order_id=str(order["id"]),
                    side=str(order["side"]),
                    quantity=float(order["qty"]),
                    status=str(order["status"]),
                    filled_quantity=float(order["filled_qty"]),
                )
                for client_order_id, order in sorted(self.orders.items())
            ),
            submit_calls=tuple(str(call[1]["client_order_id"]) for call in self.post_calls),
            cancel_calls=tuple(call[0].removeprefix("/orders/") for call in self.delete_calls),
            lookup_calls=tuple(self.lookup_calls),
            positions=(),
        )


class _CancelThroughSeamTradePort:
    """Keep reads/submits simulated while cancel crosses the production hook."""

    def __init__(self, *, broker: SyntheticBroker, seam_broker: AlpacaBroker) -> None:
        self._broker = broker
        self._seam_broker = seam_broker

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        return await self._broker.submit(leg, client_order_id=client_order_id)

    async def cancel(self, order_id: str) -> None:
        await self._seam_broker.cancel(order_id)

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        return await self._broker.get_order_by_client_order_id(client_order_id)


def _no_network_alpaca_broker() -> tuple[AlpacaBroker, _NoNetworkAlpacaSdkClient]:
    raw = _NoNetworkAlpacaSdkClient()
    client = AlpacaTradingClient(client_factory=lambda: raw)
    return AlpacaBroker(client), raw


def _with_sdk_cancel_proof(
    authority: SimulatedBrokerProof,
    sdk: SimulatedBrokerProof,
) -> SimulatedBrokerProof:
    """Attach the observed SDK cancel call to the Clerk-facing broker snapshot."""
    return SimulatedBrokerProof(
        orders=authority.orders,
        submit_calls=(*authority.submit_calls, *sdk.submit_calls),
        cancel_calls=(*authority.cancel_calls, *sdk.cancel_calls),
        lookup_calls=(*authority.lookup_calls, *sdk.lookup_calls),
        positions=authority.positions,
    )


async def lost_submit(artifacts_root: Path) -> SyntheticScenarioObservation:
    repo, clock, account_id, runs = new_repo(artifacts_root, suffix="lost-submit")
    broker = SyntheticBroker(clock)
    try:
        revision = repo.control_meta_snapshot().control_revision
        before = broker.proof()
        registry = get_fault_injection_registry()
        seam_permitted = fault_seam_permitted(registry)
        raw: _NoNetworkAlpacaSdkClient | None = None
        if seam_permitted:
            seam_broker, raw = _no_network_alpaca_broker()
            registry.arm(WriteFaultKind.POST_SDK_TIMEOUT.value)
            first = await submit_enter(
                repo,
                account_id=account_id,
                strategy_instance_id="spy-bot",
                decision_id="lost-submit",
                lifecycle_run_id=runs["spy-bot"],
                leg=synthetic_leg(),
                trade=seam_broker,
            )
        else:
            broker.submit_error = craft_write_error(WriteFaultKind.TIMEOUT.value)
            first = await submit_enter(
                repo,
                account_id=account_id,
                strategy_instance_id="spy-bot",
                decision_id="lost-submit",
                lifecycle_run_id=runs["spy-bot"],
                leg=synthetic_leg(),
                trade=broker,
            )
            broker.submit_error = None
        _require(first.order_ref is not None, "lost-submit must mint an order_ref")
        if seam_permitted:
            result = first
            after = raw.proof() if raw is not None else broker.proof()
        else:
            broker.seed_order(first.order_ref)
            result = await resolve_enter_submission(
                repo,
                order_ref=first.order_ref,
                trade=broker,
            )
            after = broker.proof()
        order = repo.order(first.order_ref)
        exact_lookups = raw.lookup_calls if raw is not None else broker.lookup_calls
        exact = exact_lookups == ([first.order_ref] if seam_permitted else [first.order_ref, first.order_ref])
        uncertain_recorded = any(
            item["transition_kind"] == "ORDER_SUBMIT_UNCERTAIN" for item in repo.transitions_for_order(first.order_ref)
        )
        seam_exercised = (
            seam_permitted
            and raw is not None
            and len(raw.post_calls) == 1
            and raw.lookup_calls == [first.order_ref]
            and registry.status()["write"] == []
        )
        passed = (
            uncertain_recorded
            and order is not None
            and order.broker_order_id is not None
            and exact
            and (not seam_permitted or seam_exercised)
        )
        limitations: tuple[FaultSeamLimitation, ...] = ()
        if not seam_permitted:
            limitations = (
                LOST_RESPONSE_LIMITATION,
                seam_not_permitted(WriteFaultKind.POST_SDK_TIMEOUT.value),
            )
        return SyntheticScenarioObservation(
            scenario_id=SyntheticScenarioId.LOST_SUBMIT_EXACT_IDENTITY,
            status=fault_seam_status(passed=passed, seam_exercised=seam_exercised),
            assertion=(
                "A production-hook response loss left accepted work unknown; recovery "
                "used only the exact Clerk-minted client identity."
            ),
            evidence=worksheet(
                repo,
                revision_before=revision,
                broker_before=before,
                broker_after=after,
                expected="landed mutation is unknown; exact lookup resolves the order",
                observed=(
                    f"seam_permitted={seam_permitted}; seam_exercised={seam_exercised}; "
                    f"post_calls={len(raw.post_calls) if raw else 0}; "
                    f"exact_lookups={exact_lookups}; "
                    f"broker_order_id={order.broker_order_id if order else None}"
                ),
                action="RESOLVE_ENTER_SUBMISSION",
                receipt_id=result.command.receipt_id,
            ),
            limitations=limitations,
            fault_seam_required=True,
            fault_seam_exercised=seam_exercised,
        )
    finally:
        repo.close()


async def lost_cancel(artifacts_root: Path) -> SyntheticScenarioObservation:
    repo, clock, account_id, runs = new_repo(artifacts_root, suffix="lost-cancel")
    broker = SyntheticBroker(clock)
    try:
        entry = await submit_enter(
            repo,
            account_id=account_id,
            strategy_instance_id="spy-bot",
            decision_id="entry",
            lifecycle_run_id=runs["spy-bot"],
            leg=synthetic_leg(quantity=10),
            trade=broker,
        )
        _require(entry.order_ref is not None, "lost-cancel entry must mint an order_ref")
        revision = repo.control_meta_snapshot().control_revision
        before = broker.proof()
        registry = get_fault_injection_registry()
        seam_permitted = fault_seam_permitted(registry)
        raw: _NoNetworkAlpacaSdkClient | None = None
        if seam_permitted:
            current_entry = repo.order(entry.order_ref)
            _require(
                current_entry is not None and current_entry.broker_order_id is not None,
                "lost-cancel seam requires the established broker-order identity",
            )
            seam_broker, raw = _no_network_alpaca_broker()
            registry.arm(
                WriteFaultKind.POST_SDK_TIMEOUT.value,
                target=current_entry.broker_order_id,
            )
            trade: BrokerTradePort = _CancelThroughSeamTradePort(
                broker=broker,
                seam_broker=seam_broker,
            )
        else:
            broker.cancel_error = craft_write_error(WriteFaultKind.TIMEOUT.value)
            trade = broker
        accepted = accept_exit(
            repo,
            account_id=account_id,
            strategy_instance_id="spy-bot",
            decision_id="exit",
            lifecycle_run_id=runs["spy-bot"],
            entry_order_ref=entry.order_ref,
        )
        _require(
            accepted.effect_operation_id is not None,
            "lost-cancel exit must mint an effect_operation_id",
        )
        cancel_source_event_at_ms = clock()
        result = await resolve_exit(
            repo,
            effect_operation_id=accepted.effect_operation_id,
            trade=trade,
        )
        effect = repo.effect_operation(accepted.effect_operation_id)
        after = broker.proof()
        if raw is not None:
            after = _with_sdk_cancel_proof(after, raw.proof())
        seam_exercised = (
            seam_permitted
            and raw is not None
            and len(raw.delete_calls) == 1
            and after.cancel_calls == (raw.delete_calls[0][0].removeprefix("/orders/"),)
            and registry.status()["write"] == []
        )
        passed = (
            effect is not None
            and effect.state == "unknown"
            and result.reducing_order_ref is None
            and (not seam_permitted or seam_exercised)
        )
        limitations: tuple[FaultSeamLimitation, ...] = ()
        if not seam_permitted:
            limitations = (
                LOST_RESPONSE_LIMITATION,
                seam_not_permitted(WriteFaultKind.POST_SDK_TIMEOUT.value),
            )
        return SyntheticScenarioObservation(
            scenario_id=SyntheticScenarioId.LOST_CANCEL_RETAINS_CUSTODY,
            status=fault_seam_status(passed=passed, seam_exercised=seam_exercised),
            assertion=(
                "The production client seam hid a completed SDK cancel response, left "
                "EXIT unknown, and submitted no reducing order."
            ),
            evidence=worksheet(
                repo,
                revision_before=revision,
                broker_before=before,
                broker_after=after,
                expected="unknown custody with no reducing order",
                observed=(
                    f"seam_permitted={seam_permitted}; seam_exercised={seam_exercised}; "
                    f"sdk_delete_calls={len(raw.delete_calls) if raw else 0}; "
                    f"structured_cancel_calls={after.cancel_calls}; "
                    f"effect_state={effect.state if effect else None}; "
                    f"reducing_order_ref={result.reducing_order_ref}"
                ),
                action="CANCEL_ENTRY",
                receipt_id=result.command.receipt_id,
                source_event_at_ms=cancel_source_event_at_ms,
            ),
            limitations=limitations,
            fault_seam_required=True,
            fault_seam_exercised=seam_exercised,
        )
    finally:
        repo.close()


__all__ = ["lost_cancel", "lost_submit"]
