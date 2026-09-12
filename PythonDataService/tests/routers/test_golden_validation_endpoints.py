"""Golden Validation's HTTP workflow over the Python-owned research schema."""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from httpx import ASGITransport

from app.main import app
from app.research.backtest_runs import repository as backtest_repo
from app.research.backtest_runs.records import record_from_payload
from app.research.persistence.db import with_connection
from tests.research.backtest_runs.payloads import engine_payload


def _requires_ephemeral_db() -> None:
    if not os.getenv("POSTGRES_URL") or os.getenv("POSTGRES_URL_IS_EPHEMERAL", "").lower() not in ("1", "true"):
        pytest.skip("live-DB endpoint tests need an ephemeral POSTGRES_URL")


async def test_designate_review_list_and_run_retention_are_one_referenceable_workflow() -> None:
    _requires_ephemeral_db()
    symbol = f"G{uuid.uuid4().hex[:6].upper()}"
    payload = engine_payload(symbol=symbol, program_version="ema-signal-v1")
    run_id = (await with_connection(backtest_repo.insert_run, record_from_payload(payload))).run_id

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        designated = await client.post(
            "/api/research/golden-validations",
            json={
                "source_run_id": run_id,
                "command_id": f"designate-{symbol}",
                "label": "AAPL research baseline",
                "rationale": "Selected after tuning the gates for this ticker.",
            },
        )
        assert designated.status_code == 201, designated.text
        candidate = designated.json()
        assert candidate["source_run_id"] == run_id
        assert candidate["state"] == "candidate" and candidate["evidence_state"] == "missing"
        assert candidate["validation_case"]["strategy"]["program_version"] == "ema-signal-v1"
        assert candidate["validation_case"]["symbol"] == symbol
        assert candidate["latest_review"] is None

        reviewed = await client.post(
            f"/api/research/golden-validations/{candidate['id']}/reviews",
            json={
                "command_id": f"review-{symbol}",
                "expected_evidence_revision": candidate["evidence_revision"],
                "decision": "accept",
                "reason": "Paper observation may proceed while parity evidence is unavailable.",
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        accepted = reviewed.json()
        assert accepted["state"] == "accepted_manual_override"
        assert accepted["latest_review"]["classification"] == "manual_override"
        assert accepted["latest_review"]["evidence_state"] == "missing"
        assert accepted["latest_review"]["quantconnect_backtest_id"] is None

        listed = await client.get("/api/research/golden-validations", params={"symbol": symbol})
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()] == [candidate["id"]]

        retained = await client.delete(f"/api/research/backtest-runs/{run_id}")
        assert retained.status_code == 409
        assert retained.json()["detail"]["code"] == "GOLDEN_VALIDATION_EVIDENCE"


async def test_endpoint_reports_idempotency_conflicts_without_mutating_the_first_designation() -> None:
    _requires_ephemeral_db()
    symbol = f"G{uuid.uuid4().hex[:6].upper()}"
    run_id = (
        await with_connection(
            backtest_repo.insert_run,
            record_from_payload(engine_payload(symbol=symbol, program_version="ema-signal-v1")),
        )
    ).run_id
    command_id = f"same-command-{symbol}"

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(
            "/api/research/golden-validations",
            json={
                "source_run_id": run_id,
                "command_id": command_id,
                "rationale": "The first immutable reason.",
            },
        )
        assert first.status_code == 201

        conflict = await client.post(
            "/api/research/golden-validations",
            json={
                "source_run_id": run_id,
                "command_id": command_id,
                "rationale": "A different meaning for the same command.",
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "GOLDEN_VALIDATION_COMMAND_CONFLICT"
