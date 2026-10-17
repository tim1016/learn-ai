"""Approval versions, retry semantics, revocation and deploy admission."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.lean_sidecar.trading_calendar import CALENDAR_IDENTITY, CALENDAR_PACKAGE_VERSION
from app.options_backtest.run import RunResult, run_sessions
from app.options_bots.golden import GoldenOptionsSetups, GoldenSetupError, SourceRun
from app.options_bots.golden_proof import GoldenProofError, result_sha256, setup_sha256
from app.options_bots.golden_repository import CommandConflict, GoldenEvent, GoldenRecord
from app.routers import golden_option_setups
from app.schemas.golden_option_setups import (
    ApproveGoldenOptionSetupRequest,
    ReproveGoldenOptionSetupRequest,
    RevokeGoldenOptionSetupRequest,
)
from tests.options_backtest._market import leg, setup
from tests.options_backtest._run_data import WED, Day, fake_data, flat, steady


class MemoryGoldenStore:
    def __init__(self) -> None:
        self.records: dict[str, GoldenRecord] = {}
        self.history: list[GoldenEvent] = []

    async def list(self, *, limit: int = 200, source_run_id: int | None = None) -> tuple[GoldenRecord, ...]:
        records = (record for record in self.records.values() if source_run_id is None or record.source_run_id == source_run_id)
        return tuple(sorted(records, key=lambda record: (record.created_at_ms, record.id), reverse=True))[:limit]

    async def get(self, qualification_id: str) -> GoldenRecord | None:
        return self.records.get(qualification_id)

    async def by_command(self, command_id: str) -> GoldenRecord | None:
        return next((row for row in self.records.values() if row.command_id == command_id), None)

    async def events(self, qualification_id: str) -> tuple[GoldenEvent, ...]:
        return tuple(event for event in self.history if event.qualification_id == qualification_id)

    async def events_for(self, qualification_ids: Sequence[str]) -> tuple[GoldenEvent, ...]:
        return tuple(event for event in self.history if event.qualification_id in qualification_ids)

    async def event_by_command(self, command_id: str) -> GoldenEvent | None:
        return next((event for event in self.history if event.command_id == command_id), None)

    async def insert(self, record: GoldenRecord) -> GoldenRecord:
        if prior := await self.by_command(record.command_id):
            if record.command_sha256 != prior.command_sha256:
                raise CommandConflict("conflict")
            return prior
        self.records[record.id] = record
        return record

    async def append(self, event: GoldenEvent) -> GoldenEvent:
        if prior := await self.event_by_command(event.command_id):
            if event.command_sha256 != prior.command_sha256:
                raise CommandConflict("conflict")
            return prior
        appended = replace(event, id=len(self.history) + 1)
        self.history.append(appended)
        return appended


@pytest.fixture
def source() -> SourceRun:
    result = run_sessions(
        setup(leg()), [WED], fake_data({WED: Day(underlying=steady("500"), options={("call", 500): flat("2", 1, 390)})}),
    )
    return SourceRun("My tested setup", result, CALENDAR_IDENTITY, CALENDAR_PACKAGE_VERSION)


def _proof(result: RunResult) -> dict[str, Any]:
    return {
        "rules_digest": "rules-v1", "setup_sha256": setup_sha256(result.setup),
        "result_sha256": result_sha256(result), "manifest": {},
        "calendar_identity": CALENDAR_IDENTITY, "calendar_package_version": CALENDAR_PACKAGE_VERSION,
    }


def _module(source: SourceRun) -> GoldenOptionsSetups:
    async def load(_run_id: int) -> SourceRun:
        return source

    return GoldenOptionsSetups(MemoryGoldenStore(), source=load, prove=_proof, rules_digest=lambda: "rules-v1")


def _approve(**changes: Any) -> ApproveGoldenOptionSetupRequest:
    return ApproveGoldenOptionSetupRequest(
        **{"run_id": 123, "note": "Reviewed exact setup", "idempotency_key": "approve-test-1", **changes}
    )


async def test_approval_freezes_exact_tested_snapshot_and_retry_reuses_its_version(source: SourceRun) -> None:
    module = _module(source)
    approved = await module.approve(_approve())
    retried = await module.approve(_approve())

    assert retried.id == approved.id
    assert approved.setup == source.result.setup
    assert approved.status == "ready"
    assert approved.source_run_id == 123
    assert len(await module.list()) == 1

    with pytest.raises(GoldenSetupError, match="different request"):
        await module.approve(_approve(note="A different approval"))


async def test_a_mismatched_review_or_failed_proof_publishes_nothing(source: SourceRun) -> None:
    module = _module(source)
    with pytest.raises(GoldenSetupError, match="different from"):
        await module.approve(_approve(expected_setup_sha256="0" * 64))

    def fail(_result: RunResult) -> dict[str, Any]:
        raise GoldenProofError("changed source input")

    module.prove = fail
    with pytest.raises(GoldenSetupError, match="changed source"):
        await module.approve(_approve())
    assert await module.list() == []


async def test_running_rule_change_holds_entry_until_successful_reproof_without_rewriting_approval(source: SourceRun) -> None:
    module = _module(source)
    approved = await module.approve(_approve())
    module.rules_digest = lambda: "rules-v2"
    assert (await module.get(approved.id)).status == "stale"
    with pytest.raises(GoldenSetupError) as held:
        await module.ready(approved.id)
    assert held.value.code == "GOLDEN_SETUP_STALE"

    module.reprove_run = lambda _source, proof: {**proof, "rules_digest": "rules-v2"}
    reproved = await module.reprove(approved.id, ReproveGoldenOptionSetupRequest(idempotency_key="reprove-test-1"))

    assert reproved.status == "ready"
    assert reproved.rules_digest == "rules-v2"
    assert (await module.store.get(approved.id)).rules_digest == "rules-v1"
    assert (await module.ready(approved.id)).setup_sha256 == approved.setup_sha256


async def test_revocation_is_durable_blocks_reproof_and_cannot_be_overridden_by_retry(source: SourceRun) -> None:
    module = _module(source)
    approved = await module.approve(_approve())
    request = RevokeGoldenOptionSetupRequest(reason="Research withdrawn", idempotency_key="revoke-test-1")
    revoked = await module.revoke(approved.id, request)
    retried = await module.revoke(approved.id, request)

    assert retried.status == revoked.status == "revoked"
    assert len(retried.events) == 1
    with pytest.raises(GoldenSetupError) as held:
        await module.ready(approved.id)
    assert held.value.code == "GOLDEN_SETUP_REVOKED"
    with pytest.raises(GoldenSetupError):
        await module.reprove(approved.id, ReproveGoldenOptionSetupRequest(idempotency_key="reprove-test-1"))


async def test_http_approval_and_unknown_version_have_typed_contracts(source: SourceRun) -> None:
    module = _module(source)
    app = FastAPI()
    app.include_router(golden_option_setups.router, prefix="/api/research/golden-option-setups")
    app.dependency_overrides[golden_option_setups.service] = lambda: module
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/research/golden-option-setups/approve", json=_approve().model_dump())
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["setup"]["underlying"] == "SPY"
        assert body["evidence"]["limitations"]
        missing = await client.get("/api/research/golden-option-setups/ogs_" + "0" * 32)
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "GOLDEN_SETUP_NOT_FOUND"


async def test_run_filter_finds_approval_beyond_the_recent_list(source: SourceRun) -> None:
    module = _module(source)
    approved = await module.approve(_approve())
    record = module.store.records[approved.id]
    for number in range(201):
        newer = replace(record, id=f"ogs_{number:032x}", source_run_id=1000 + number, created_at_ms=record.created_at_ms + number + 1)
        module.store.records[newer.id] = newer
    app = FastAPI()
    app.include_router(golden_option_setups.router, prefix="/api/research/golden-option-setups")
    app.dependency_overrides[golden_option_setups.service] = lambda: module
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        recent = await client.get("/api/research/golden-option-setups")
        assert approved.id not in {item["id"] for item in recent.json()}
        filtered = await client.get("/api/research/golden-option-setups", params={"source_run_id": 123})

        assert filtered.status_code == 200
        assert [item["id"] for item in filtered.json()] == [approved.id]


async def test_catalog_shares_one_running_digest_and_bulk_event_read(source: SourceRun) -> None:
    module = _module(source)
    for number in range(3):
        approved = await module.approve(_approve(idempotency_key=f"approve-list-{number}"))
    await module.revoke(approved.id, RevokeGoldenOptionSetupRequest(reason="Owner withdrew approval", idempotency_key="revoke-list-version"))
    digest_reads = 0
    event_reads = 0
    prior_events = module.store.events

    def digest() -> str:
        nonlocal digest_reads
        digest_reads += 1
        return "rules-v1"

    async def events(qualification_id: str) -> tuple[GoldenEvent, ...]:
        nonlocal event_reads
        event_reads += 1
        return await prior_events(qualification_id)

    module.rules_digest, module.store.events = digest, events

    listed = await module.list()

    assert sorted(item.status for item in listed) == ["ready", "ready", "revoked"]
    assert digest_reads == 1
    assert event_reads == 0
