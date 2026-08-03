"""Clerk ``resolve_custody()`` orchestration tests (task 2.1, Style A: direct clerk).

Composes the already-landed read-only ``custody_diagnosis()`` (task 1.1/1.2)
with the existing recovery verbs (``reconcile_once``,
``record_inventory_baseline``, ``clear_hold``) behind a snapshot guard. These
tests assert the operator's reason reaches the journal and that a stale
snapshot token is rejected before any mutation.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
import responses

from app.broker.alpaca.broker import AlpacaBroker
from app.broker.alpaca.clerk import diagnosis
from app.broker.alpaca.clerk import journal as journal_module
from app.broker.alpaca.clerk.clerk import (
    AlpacaClerk,
    reset_alpaca_clerk_for_testing,
    set_alpaca_clerk,
)
from app.broker.alpaca.clerk.models import ClerkEntryKind
from app.broker.alpaca.client import AlpacaTradingClient
from app.broker.alpaca.config import AlpacaSettings, reset_alpaca_settings_for_testing
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
        await clerk.resolve_custody(operator="ops", reason="x", snapshot_version="stale-token")


async def test_resolve_already_in_sync_is_idempotent_noop() -> None:
    broker = _FakeBroker(orders=[], positions=[])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    diag = await clerk.custody_diagnosis()

    receipt = await clerk.resolve_custody(
        operator="ops", reason="no-op check", snapshot_version=diag.snapshot_version
    )

    assert receipt.resolved is True
    assert receipt.in_sync is True
    assert receipt.steps_executed == ()


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
