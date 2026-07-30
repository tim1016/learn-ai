"""HTTP seam tests for the operator-gated evidence endpoint (S4, §14).

Drives the account-scoped evidence route through the FastAPI HTTP surface with
journal fixtures. Asserts:
- A GET returns a bounded EvidencePage.
- Every GET appends a server-side audit entry to the per-account audit log.
- Entries are filtered to the requesting bot's namespace only.
- Pagination cursor advances correctly.
- page_size is capped at PAGE_SIZE_MAX.
- Unknown broker → 404; account mismatch → 404.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.alpaca.clerk import reset_alpaca_clerk_for_testing, set_alpaca_clerk
from app.broker.alpaca.clerk.journal import (
    OrderJournal,
    get_clerk_settings,
    reset_clerk_settings_for_testing,
)
from app.broker.alpaca.clerk.models import ClerkStatus, HoldState, ReconciliationSummary
from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.routers.broker_v2_panel import router
from app.schemas.broker_bots import BotStatusView
from app.services.bot_runner import set_bot_task_registry
from app.services.broker_v2_panel.evidence_service import PAGE_SIZE_MAX
from tests.broker.v2panel.fixtures import (
    ACCT,
    OTHER_SID,
    SID,
    intent_entry,
    order_ref,
    submit_acked_entry,
)

_T0 = 1_700_000_000_000


class _FakeAccount:
    account_id = ACCT


class _FakeReadPort:
    broker_id = "alpaca"

    async def get_account(self) -> _FakeAccount:
        return _FakeAccount()

    def capabilities(self) -> None:  # pragma: no cover
        raise NotImplementedError


def _make_status(sid: str) -> BotStatusView:
    return BotStatusView(
        strategy_instance_id=sid,
        broker="alpaca",
        symbol="SPY",
        mode="log_only",
        quantity=1,
        running=True,
        phase="ON_DUTY",
        desired_state="RUNNING",
        active_run_id=None,
        duty_outcome=None,
        binding_created_at_ms=_T0,
        last_transition_at_ms=None,
    )


class _FakeRegistry:
    def list_bots(self, broker: str) -> list[BotStatusView]:
        return [_make_status(SID)]

    def status(self, broker: str, sid: str) -> BotStatusView:
        from app.services.bot_runner import BotRunnerError

        if sid not in (SID, OTHER_SID):
            raise BotRunnerError(f"Unknown bot {sid}", detail=None)
        return _make_status(sid)


class _FakeClerk:
    async def status(self) -> ClerkStatus:
        return ClerkStatus(
            account_id=ACCT,
            hold_state=HoldState(
                active=False, reason_code=None, reason=None, since_ms=None
            ),
            reconciliation=ReconciliationSummary(
                verdict="clean", last_sweep_at_ms=_T0, outstanding_intents=0
            ),
            channel_health=[],
        )


@pytest.fixture()
def app_and_tmp(tmp_path: Path, monkeypatch):
    # Redirect clerk journal to an isolated temp dir (matches pattern in test_panel_router.py).
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path))
    reset_clerk_settings_for_testing()
    reset_broker_registry_for_testing()
    reset_alpaca_clerk_for_testing()

    get_broker_registry().register(_FakeReadPort())  # type: ignore[arg-type]
    set_alpaca_clerk(_FakeClerk())
    set_bot_task_registry(_FakeRegistry())

    fast_app = FastAPI()
    fast_app.include_router(router)

    try:
        yield fast_app, tmp_path
    finally:
        set_bot_task_registry(None)
        set_alpaca_clerk(None)
        reset_broker_registry_for_testing()
        reset_clerk_settings_for_testing()


def _write_journal(tmp_path: Path, entries: list) -> None:
    journal = OrderJournal(account_id=ACCT, root=get_clerk_settings().dir)
    for entry in entries:
        journal.append(entry)


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evidence_returns_bot_entries_newest_first(app_and_tmp) -> None:
    fast_app, tmp_path = app_and_tmp
    _write_journal(
        tmp_path,
        [
            intent_entry(sid=SID, intent="i1", ts_ms=_T0),
            intent_entry(sid=SID, intent="i2", ts_ms=_T0 + 1000),
            intent_entry(sid=OTHER_SID, intent="i3", ts_ms=_T0 + 2000),
        ],
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/evidence"
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["strategy_instance_id"] == SID
    assert body["total_entries"] == 2
    entries = body["entries"]
    assert len(entries) == 2
    # Newest first: i2 before i1.
    assert entries[0]["intent_id"] == "i2"
    assert entries[1]["intent_id"] == "i1"


@pytest.mark.asyncio
async def test_evidence_audit_entry_written(app_and_tmp) -> None:
    fast_app, tmp_path = app_and_tmp
    _write_journal(tmp_path, [intent_entry(sid=SID, intent="i1", ts_ms=_T0)])

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/evidence"
        )

    assert resp.status_code == 200
    audit_path = tmp_path / "accounts" / ACCT / "evidence_audit.jsonl"
    assert audit_path.exists(), "Audit log must be written on every evidence read"
    lines = audit_path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["strategy_instance_id"] == SID
    assert entry["account_id"] == ACCT
    assert entry["entries_returned"] == 1


@pytest.mark.asyncio
async def test_evidence_pagination_cursor(app_and_tmp) -> None:
    fast_app, tmp_path = app_and_tmp
    _write_journal(
        tmp_path,
        [intent_entry(sid=SID, intent=f"i{n}", ts_ms=_T0 + n * 1000) for n in range(5)],
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app), base_url="http://test"
    ) as client:
        resp1 = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/evidence",
            params={"page_size": 2},
        )
        assert resp1.status_code == 200
        body1 = resp1.json()
        assert len(body1["entries"]) == 2
        assert body1["next_cursor"] == 2

        resp2 = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/evidence",
            params={"page_size": 2, "cursor": body1["next_cursor"]},
        )
        assert resp2.status_code == 200
        body2 = resp2.json()
        assert len(body2["entries"]) == 2
        # No overlap between pages.
        ids1 = {e["intent_id"] for e in body1["entries"]}
        ids2 = {e["intent_id"] for e in body2["entries"]}
        assert ids1.isdisjoint(ids2)


@pytest.mark.asyncio
async def test_evidence_page_size_capped_at_max(app_and_tmp) -> None:
    fast_app, tmp_path = app_and_tmp
    _write_journal(
        tmp_path,
        [
            intent_entry(sid=SID, intent=f"i{n}", ts_ms=_T0 + n)
            for n in range(PAGE_SIZE_MAX + 10)
        ],
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/evidence",
            params={"page_size": PAGE_SIZE_MAX + 99},
        )
    assert resp.status_code == 200
    assert len(resp.json()["entries"]) <= PAGE_SIZE_MAX


@pytest.mark.asyncio
async def test_evidence_transaction_ref_filter(app_and_tmp) -> None:
    fast_app, tmp_path = app_and_tmp
    _write_journal(
        tmp_path,
        [
            intent_entry(sid=SID, intent="i1", ts_ms=_T0),
            submit_acked_entry(sid=SID, intent="i1", ts_ms=_T0 + 100),
            intent_entry(sid=SID, intent="i2", ts_ms=_T0 + 200),
        ],
    )
    ref = order_ref(SID, "i1")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/evidence",
            params={"transaction_ref": ref},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["transaction_ref"] == ref
    assert all(e["order_ref"] == ref for e in body["entries"])
    assert body["total_entries"] == 2


@pytest.mark.asyncio
async def test_evidence_empty_journal_returns_empty_page(app_and_tmp) -> None:
    fast_app, _tmp_path = app_and_tmp

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/evidence"
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["entries"] == []
    assert body["total_entries"] == 0
    assert body["next_cursor"] is None


@pytest.mark.asyncio
async def test_evidence_multiple_reads_produce_multiple_audit_entries(
    app_and_tmp,
) -> None:
    fast_app, tmp_path = app_and_tmp
    _write_journal(tmp_path, [intent_entry(sid=SID, intent="i1", ts_ms=_T0)])

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app), base_url="http://test"
    ) as client:
        await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/evidence")
        await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/evidence")

    audit_path = tmp_path / "accounts" / ACCT / "evidence_audit.jsonl"
    lines = audit_path.read_text().strip().splitlines()
    assert len(lines) == 2


@pytest.mark.asyncio
async def test_evidence_unknown_broker_returns_404(app_and_tmp) -> None:
    fast_app, _ = app_and_tmp
    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            f"/api/brokers/unknown_broker/accounts/{ACCT}/bots/{SID}/evidence"
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_evidence_account_mismatch_returns_404(app_and_tmp) -> None:
    fast_app, _ = app_and_tmp
    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            f"/api/brokers/alpaca/accounts/wrong-account/bots/{SID}/evidence"
        )
    assert resp.status_code == 404
