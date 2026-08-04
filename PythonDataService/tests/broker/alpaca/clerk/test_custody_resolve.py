"""Clerk ``resolve_custody()`` orchestration tests (task 2.1, Style A: direct clerk).

Composes the already-landed read-only ``custody_diagnosis()`` (task 1.1/1.2)
with the existing recovery verbs (``reconcile_once``,
``record_inventory_baseline``, ``clear_hold``) behind a snapshot guard. These
tests assert the operator's reason reaches the journal and that a stale
snapshot token is rejected before any mutation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator

import pytest
import responses

from app.broker.alpaca.broker import AlpacaBroker
from app.broker.alpaca.clerk import diagnosis
from app.broker.alpaca.clerk import journal as journal_module
from app.broker.alpaca.clerk.clerk import (
    AlpacaClerk,
    InventoryBaselineRefusedError,
    reset_alpaca_clerk_for_testing,
    set_alpaca_clerk,
)
from app.broker.alpaca.clerk.models import ChannelHealth, ClerkEntryKind
from app.broker.alpaca.clerk.stream_health import StreamHealthGate
from app.broker.alpaca.client import AlpacaTradingClient
from app.broker.alpaca.config import AlpacaSettings, reset_alpaca_settings_for_testing
from app.broker.contract.errors import BrokerSubmissionHeld, BrokerUnavailable
from app.broker.contract.models import BrokerOrderEvent, BrokerOrderLeg, BrokerOrderRequest
from tests.broker.alpaca.clerk.test_clerk_reconciliation import (
    _clerk_root,  # noqa: F401 -- autouse fixture, imported for its side effect
    _FakeBroker,
    _fixed_clock,
    _position,
)
from tests.broker.alpaca.clerk.test_clerk_status_endpoint import (
    _ACCOUNT_BODY,
    _BASE,
    _get,
    _one_spy_position_body,
    _post,
)


@pytest.fixture
def _alpaca_clerk(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Wire a fresh Clerk over the real ``AlpacaBroker`` (endpoint seam tests).

    Mirrors ``test_clerk_status_endpoint._alpaca_clerk`` — duplicated locally
    rather than imported, since a non-autouse fixture used as an explicit test
    parameter can't be re-exported across modules without ruff flagging it as
    an unused/redefined import.
    """
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path))
    journal_module.reset_clerk_settings_for_testing()
    reset_alpaca_settings_for_testing()
    alpaca_settings = AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper")
    broker = AlpacaBroker(AlpacaTradingClient(settings=alpaca_settings))
    set_alpaca_clerk(AlpacaClerk(read=broker, trade=broker))
    yield
    reset_alpaca_clerk_for_testing()
    journal_module.reset_clerk_settings_for_testing()
    reset_alpaca_settings_for_testing()


async def test_resolve_adopts_baseline_and_journals_operator_reason() -> None:
    broker = _FakeBroker(orders=[], positions=[_position(symbol="SPY", quantity=1, side="long")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    diag = await clerk.custody_diagnosis()

    receipt = await clerk.resolve_custody(
        operator="ops",
        reason="07-31 run was killed mid-fill; adopting broker truth.",
        snapshot_version=diag.snapshot_version,
        idempotency_key="baseline-1",
    )

    assert receipt.resolved is True
    assert receipt.in_sync is True
    # The operator comment is journaled on the baseline row.
    baseline = [
        e for e in clerk._journal.read_entries() if e.kind == ClerkEntryKind.BROKER_EVIDENCE_BASELINE  # type: ignore[union-attr]
    ]
    assert baseline[-1].operator == "ops"
    assert "adopting broker truth" in baseline[-1].reason


async def test_resolve_rejects_stale_snapshot() -> None:
    broker = _FakeBroker(orders=[], positions=[_position(symbol="SPY", quantity=1, side="long")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)

    with pytest.raises(diagnosis.CustodySnapshotChangedError):
        await clerk.resolve_custody(
            operator="ops",
            reason="x",
            snapshot_version="stale-token",
            idempotency_key="stale-1",
        )


async def test_resolve_already_in_sync_is_idempotent_noop() -> None:
    broker = _FakeBroker(orders=[], positions=[])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    diag = await clerk.custody_diagnosis()

    receipt = await clerk.resolve_custody(
        operator="ops",
        reason="no-op check",
        snapshot_version=diag.snapshot_version,
        idempotency_key="noop-1",
    )

    assert receipt.resolved is True
    assert receipt.in_sync is True
    assert receipt.steps_executed == ()


async def test_resolve_replays_exact_receipt_without_reapplying_baseline() -> None:
    broker = _FakeBroker(
        orders=[], positions=[_position(symbol="SPY", quantity=1, side="long")]
    )
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    snapshot = (await clerk.custody_diagnosis()).snapshot_version

    first = await clerk.resolve_custody(
        operator="ops",
        reason="adopt broker truth",
        snapshot_version=snapshot,
        idempotency_key="lost-response",
    )
    replay = await clerk.resolve_custody(
        operator="ops",
        reason="adopt broker truth",
        snapshot_version=snapshot,
        idempotency_key="lost-response",
    )

    assert replay == first
    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    assert sum(
        entry.kind is ClerkEntryKind.BROKER_EVIDENCE_BASELINE for entry in entries
    ) == 1


async def test_resolve_replays_receipt_after_clerk_restart() -> None:
    broker = _FakeBroker(
        orders=[], positions=[_position(symbol="SPY", quantity=1, side="long")]
    )
    first_clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    snapshot = (await first_clerk.custody_diagnosis()).snapshot_version
    first = await first_clerk.resolve_custody(
        operator="ops",
        reason="adopt broker truth",
        snapshot_version=snapshot,
        idempotency_key="restart-replay",
    )

    restarted = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    replay = await restarted.resolve_custody(
        operator="ops",
        reason="a retry may not change the original audit comment",
        snapshot_version="the snapshot is intentionally obsolete",
        idempotency_key="restart-replay",
    )

    assert replay == first


async def test_concurrent_duplicate_resolution_executes_once() -> None:
    broker = _FakeBroker(
        orders=[], positions=[_position(symbol="SPY", quantity=1, side="long")]
    )
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    snapshot = (await clerk.custody_diagnosis()).snapshot_version
    broker.orders_started = asyncio.Event()
    broker.release_orders = asyncio.Event()

    first_task = asyncio.create_task(
        clerk.resolve_custody(
            operator="ops",
            reason="adopt broker truth",
            snapshot_version=snapshot,
            idempotency_key="concurrent-replay",
        )
    )
    await broker.orders_started.wait()
    second_task = asyncio.create_task(
        clerk.resolve_custody(
            operator="ops",
            reason="adopt broker truth",
            snapshot_version=snapshot,
            idempotency_key="concurrent-replay",
        )
    )
    await asyncio.sleep(0)
    assert second_task.done() is False

    broker.release_orders.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert second == first
    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    assert sum(
        entry.kind is ClerkEntryKind.BROKER_EVIDENCE_BASELINE for entry in entries
    ) == 1


async def test_resolution_lock_prevents_new_foreign_event_from_being_cleared() -> None:
    broker = _FakeBroker(orders=[], positions=[])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    await clerk.record_lifecycle_event(
        client_order_id="foreign-before-diagnosis",
        event=BrokerOrderEvent(
            event_type="fill", occurred_at_ms=_fixed_clock(), price=1.0, quantity=1.0
        ),
        event_key="foreign-before",
    )
    snapshot = (await clerk.custody_diagnosis()).snapshot_version
    broker.orders_started = asyncio.Event()
    broker.release_orders = asyncio.Event()

    resolve_task = asyncio.create_task(
        clerk.resolve_custody(
            operator="ops",
            reason="account reviewed",
            snapshot_version=snapshot,
            idempotency_key="hold-race",
        )
    )
    await broker.orders_started.wait()
    lifecycle_task = asyncio.create_task(
        clerk.record_lifecycle_event(
            client_order_id="foreign-during-resolution",
            event=BrokerOrderEvent(
                event_type="fill",
                occurred_at_ms=_fixed_clock(),
                price=1.0,
                quantity=1.0,
            ),
            event_key="foreign-during",
        )
    )
    await asyncio.sleep(0)
    assert lifecycle_task.done() is False

    broker.release_orders.set()
    await resolve_task
    await lifecycle_task

    assert clerk.is_on_hold() is True


async def test_reconcile_only_resolution_persists_operator_reason() -> None:
    broker = _FakeBroker(
        orders=[],
        positions=[],
        list_error=BrokerUnavailable("down", broker="alpaca", detail="5xx"),
    )
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    snapshot = (await clerk.custody_diagnosis()).snapshot_version

    receipt = await clerk.resolve_custody(
        operator="ops",
        reason="broker outage acknowledged",
        snapshot_version=snapshot,
        idempotency_key="reconcile-audit",
    )

    assert [step.action_id for step in receipt.steps_executed] == ["reconcile_now"]
    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    audits = [
        entry
        for entry in entries
        if entry.effect_operation_id == "custody-resolution:reconcile-audit"
    ]
    assert len(audits) == 1
    assert audits[0].operator == "ops"
    assert audits[0].reason == "broker outage acknowledged"


async def test_inventory_baseline_execution_rechecks_that_every_bot_is_stopped() -> None:
    broker = _FakeBroker(orders=[], positions=[_position()])
    clerk = AlpacaClerk(
        read=broker,
        trade=broker,
        clock=_fixed_clock,
        bot_running_probe=lambda: True,
    )
    assert await clerk.reconcile_once() == "missing_intent"

    with pytest.raises(InventoryBaselineRefusedError, match="bot is running"):
        await clerk.record_inventory_baseline(operator="ops", reason="unsafe attempt")


async def test_clear_hold_execution_rechecks_channel_health() -> None:
    # clear_hold's channel-health recheck only applies to a STREAM_HEALTH_HOLD
    # (P0-2 fix: the required proof is dispatched by the hold's own reason
    # code) — so this hold must be raised through a genuinely broken channel,
    # not a foreign lifecycle event (which would journal an
    # UNEXPLAINED_ORDER_HOLD and take the reconciliation-proof path instead).
    unhealthy = ChannelHealth(
        stream="execution",
        healthy=False,
        reason="trade updates disconnected",
        observed_at_ms=_fixed_clock(),
    )
    healthy = ChannelHealth(
        stream="market_data",
        healthy=True,
        observed_at_ms=_fixed_clock(),
    )
    gate = StreamHealthGate(market_data=lambda: healthy, execution=lambda: unhealthy)
    broker = _FakeBroker(orders=[], positions=[])
    clerk = AlpacaClerk(
        read=broker, trade=broker, clock=_fixed_clock, stream_health=gate
    )
    with pytest.raises(BrokerSubmissionHeld):
        await clerk.submit(
            BrokerOrderRequest(
                operator="inkant",
                legs=[BrokerOrderLeg(symbol="SPY", side="buy", quantity=1)],
            )
        )

    with pytest.raises(InventoryBaselineRefusedError, match="channels are unhealthy"):
        await clerk.clear_hold(operator="ops", reason="unsafe attempt")

    assert clerk.is_on_hold() is True


# ── HTTP endpoint seam (task 2.2, Style B: ASGITransport) ───────────────────


def _wire_one_spy_position() -> None:
    """Register the account/orders/positions ``responses`` mocks this suite reuses.

    One open SPY position with no working orders is a ``resolvable_now``
    attribution mismatch — enough to drive the diagnosis endpoint and, on the
    happy path, a full resolve.
    """
    responses.add(responses.GET, f"{_BASE}/v2/account", body=_ACCOUNT_BODY, status=200)
    responses.add(responses.GET, f"{_BASE}/v2/orders", body="[]", status=200)
    responses.add(
        responses.GET, f"{_BASE}/v2/positions", body=_one_spy_position_body(), status=200
    )


@responses.activate
async def test_resolve_endpoint_requires_the_token(_alpaca_clerk: None) -> None:
    _wire_one_spy_position()
    diag = (await _get("/api/brokers/alpaca/clerk/custody-diagnosis")).json()

    response = await _post(
        "/api/brokers/alpaca/clerk/resolve",
        {
            "reason": "adopting broker truth",
            "snapshot_version": diag["snapshot_version"],
            "confirmation_token": "NOPE",
            "idempotency_key": "k1",
        },
    )

    assert response.status_code == 422


@responses.activate
async def test_resolve_endpoint_409_on_stale_snapshot(_alpaca_clerk: None) -> None:
    _wire_one_spy_position()

    response = await _post(
        "/api/brokers/alpaca/clerk/resolve",
        {
            "reason": "x",
            "snapshot_version": "stale",
            "confirmation_token": "RESOLVE",
            "idempotency_key": "k2",
        },
    )

    assert response.status_code == 409


@responses.activate
async def test_resolve_endpoint_rejects_blank_reason(_alpaca_clerk: None) -> None:
    _wire_one_spy_position()
    diag = (await _get("/api/brokers/alpaca/clerk/custody-diagnosis")).json()

    response = await _post(
        "/api/brokers/alpaca/clerk/resolve",
        {
            "reason": "   ",
            "snapshot_version": diag["snapshot_version"],
            "confirmation_token": "RESOLVE",
            "idempotency_key": "k3",
        },
    )

    assert response.status_code == 422


@responses.activate
async def test_resolve_endpoint_happy_path_resolves(_alpaca_clerk: None) -> None:
    _wire_one_spy_position()
    diag = (await _get("/api/brokers/alpaca/clerk/custody-diagnosis")).json()

    response = await _post(
        "/api/brokers/alpaca/clerk/resolve",
        {
            "reason": "adopting broker truth",
            "snapshot_version": diag["snapshot_version"],
            "confirmation_token": "RESOLVE",
            "idempotency_key": "k4",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["resolved"] is True
