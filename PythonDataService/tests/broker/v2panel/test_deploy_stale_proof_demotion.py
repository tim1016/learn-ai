"""Stale-proof demotion tests for the Alpaca paper-deploy strategy catalog (#1698).

A human-validated strategy whose recorded proof no longer re-verifies is
demoted to a ``blocked``, non-selectable row — never silently dropped. These
tests pin that contract at the pure projection layer (``_strategy_views``)
and at the HTTP route layer (the deploy view, the deploy preflight, and the
admission-preview preflight all applying the identical rule).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.schemas.strategy_validation import StrategyValidationEntry, StrategyValidationFlagRequest
from app.services.broker_v2_panel import panel_data_source
from app.services.broker_v2_panel.paper_deploy_service import _strategy_views
from app.services.strategy_validation_manifest import (
    append_strategy_validation_flag_event,
    load_strategy_validation_entries,
    strategy_registry_seeds,
)
from tests.broker.v2panel.conftest import _BODY, _accepted_deploy_entry, _FakeDeployRegistry
from tests.broker.v2panel.fixtures import ACCT


def _entries_for(*strategy_keys: str) -> list[StrategyValidationEntry]:
    """Real committed manifest entries, restricted to the given strategy keys."""
    seeds = strategy_registry_seeds()
    return [entry for entry in load_strategy_validation_entries(seeds) if entry.strategy_key in strategy_keys]


def _synthetic_blocked_entry(strategy_key: str) -> StrategyValidationEntry:
    """Force one real validated entry into a deterministic blocked state.

    Corrupts the audit-copy hash of a currently-real, currently-accepted
    entry so the row demotes to ``blocked`` regardless of whether that
    strategy's real committed proof happens to be stale right now —
    repairing the real proof must never make this fixture (or the tests
    that use it) flaky.
    """
    (entry,) = _entries_for(strategy_key)
    return entry.model_copy(update={"audit_copy_sha256": "0" * 64})


def test_deploy_demotes_manifest_proof_that_differs_from_accepted_snapshot() -> None:
    entry = _accepted_deploy_entry()
    changed = entry.model_copy(update={"qc_cloud_backtest_id": "a-different-qc-backtest-id"})

    rows = _strategy_views([changed])

    assert [row.strategy_key for row in rows] == [entry.strategy_key]
    row = rows[0]
    assert row.evidence_status == "blocked"
    assert row.selectable is False
    assert row.blocked_explanation is not None
    assert "no longer matches the proof snapshot" in row.blocked_explanation


def test_deploy_reverifies_the_accepted_audit_copy_hash() -> None:
    entry = _accepted_deploy_entry()
    event = entry.current_flag_event
    assert event is not None
    bad_hash = "0" * 64
    changed_snapshot = event.evidence_snapshot.model_copy(update={"audit_copy_sha256": bad_hash})
    changed_event = event.model_copy(update={"evidence_snapshot": changed_snapshot})
    changed = entry.model_copy(
        update={
            "audit_copy_sha256": bad_hash,
            "current_flag_event": changed_event,
        }
    )

    rows = _strategy_views([changed])

    assert [row.strategy_key for row in rows] == [entry.strategy_key]
    row = rows[0]
    assert row.evidence_status == "blocked"
    assert row.selectable is False
    assert row.blocked_explanation is not None
    assert "audit copy" in row.blocked_explanation
    assert "no longer matches its recorded hash" in row.blocked_explanation


def test_strategy_views_admissible_modes_track_selectable() -> None:
    """#1702: every row admits dry_run (runtime-backed); paper tracks
    selectable exactly — accepted and evidence-only rows admit both modes,
    a blocked row admits dry_run only."""
    accepted = _accepted_deploy_entry()
    blocked = _synthetic_blocked_entry("deployment_validation")

    rows = _strategy_views([accepted, blocked])

    rows_by_key = {row.strategy_key: row for row in rows}
    accepted_row = rows_by_key[accepted.strategy_key]
    blocked_row = rows_by_key["deployment_validation"]
    assert accepted_row.evidence_status == "accepted"
    assert accepted_row.selectable is True
    assert accepted_row.admissible_modes == ("dry_run", "paper")
    assert blocked_row.evidence_status == "blocked"
    assert blocked_row.selectable is False
    assert blocked_row.admissible_modes == ("dry_run",)


def test_strategy_views_evidence_only_row_is_paper_admissible_with_no_override(tmp_path: Path) -> None:
    """#1702: an evidence-only row is Paper-selectable — its behavioral
    verdict no longer gates Paper, only the human-validated flag does."""
    seeds = strategy_registry_seeds()
    flag_events_path = tmp_path / "strategy-validation" / "flag-events.json"
    append_strategy_validation_flag_event(
        "sma_crossover",
        StrategyValidationFlagRequest(
            flag="validated",
            reason="Test-only human validation without accepted equivalence evidence.",
        ),
        seeds,
        flag_events_path=flag_events_path,
        flagged_by="test:mode-tiered-admission",
        now_ms=1_700_000_000_000,
    )
    (entry,) = [
        entry
        for entry in load_strategy_validation_entries(seeds, flag_events_path=flag_events_path)
        if entry.strategy_key == "sma_crossover"
    ]

    rows = _strategy_views([entry])

    assert len(rows) == 1
    row = rows[0]
    assert row.evidence_status == "evidence_only"
    assert row.selectable is True
    assert row.admissible_modes == ("dry_run", "paper")
    assert row.override_explanation is not None


def test_deploy_demotes_accepted_event_with_gating_divergence() -> None:
    entry = _accepted_deploy_entry()
    event = entry.current_flag_event
    assert event is not None
    changed_event = event.model_copy(
        update={
            "behavioral_equivalence": event.behavioral_equivalence.model_copy(
                update={"gating_divergence_counts": {"DECISION_MISMATCH": 1}}
            )
        }
    )
    changed = entry.model_copy(update={"current_flag_event": changed_event})

    rows = _strategy_views([changed])

    assert [row.strategy_key for row in rows] == [entry.strategy_key]
    row = rows[0]
    assert row.evidence_status == "blocked"
    assert row.selectable is False
    assert row.blocked_explanation is not None
    assert "gating divergence" in row.blocked_explanation
    assert "DECISION_MISMATCH" in row.blocked_explanation


@pytest.mark.asyncio
async def test_deploy_view_shows_blocked_strategy_but_stays_eligible_when_another_is_selectable(
    deploy_app: tuple[FastAPI, _FakeDeployRegistry],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast_app, _registry = deploy_app
    monkeypatch.setattr(
        panel_data_source,
        "load_strategy_validation_entries",
        lambda _registry: [_accepted_deploy_entry(), _synthetic_blocked_entry("deployment_validation")],
    )

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy")

    body = response.json()
    rows_by_key = {row["strategy_key"]: row for row in body["strategies"]}
    assert rows_by_key["deployment_validation"]["evidence_status"] == "blocked"
    assert rows_by_key["deployment_validation"]["selectable"] is False
    assert rows_by_key["deployment_validation"]["blocked_explanation"]
    assert rows_by_key["ema_crossover_signal"]["evidence_status"] == "accepted"
    assert rows_by_key["ema_crossover_signal"]["selectable"] is True
    assert body["eligibility"]["eligible"] is True


@pytest.mark.asyncio
async def test_deploy_refuses_non_selectable_strategy_with_typed_conflict(
    deploy_app: tuple[FastAPI, _FakeDeployRegistry],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast_app, registry = deploy_app
    monkeypatch.setattr(
        panel_data_source,
        "load_strategy_validation_entries",
        lambda _registry: [_accepted_deploy_entry(), _synthetic_blocked_entry("deployment_validation")],
    )

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json={**_BODY, "strategy_key": "deployment_validation"},
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["outcome"] == "conflict"
    assert detail["receipt_id"] is None
    assert detail["recorded_at_ms"] > 0
    assert "not currently selectable" in detail["message"]
    assert detail["admission"] is None
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_deploy_admits_blocked_strategy_for_dry_run_but_refuses_paper(
    deploy_app: tuple[FastAPI, _FakeDeployRegistry],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1702: a blocked row (proof no longer verifies) is Dry-Run-admissible —
    Dry Run ignores the human-validated flag and behavioral evidence
    entirely — even though it remains Paper-unselectable (regression, same
    typed conflict as the Paper-refusal test above)."""
    fast_app, registry = deploy_app
    monkeypatch.setattr(
        panel_data_source,
        "load_strategy_validation_entries",
        lambda _registry: [_accepted_deploy_entry(), _synthetic_blocked_entry("deployment_validation")],
    )

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        dry_run_response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json={**_BODY, "strategy_key": "deployment_validation", "execution_mode": "dry_run"},
        )

    assert dry_run_response.status_code == 201
    assert dry_run_response.json()["execution_mode"] == "dry_run"
    assert registry.deploy_calls[0]["strategy_key"] == "deployment_validation"
    assert registry.deploy_calls[0]["mode"] == "dry_run"


@pytest.mark.asyncio
async def test_admission_preview_refuses_non_selectable_strategy_with_the_same_typed_conflict_as_deploy(
    deploy_app: tuple[FastAPI, _FakeDeployRegistry],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast_app, _registry = deploy_app
    monkeypatch.setattr(
        panel_data_source,
        "load_strategy_validation_entries",
        lambda _registry: [_accepted_deploy_entry(), _synthetic_blocked_entry("deployment_validation")],
    )

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/admission",
            json={**_BODY, "strategy_key": "deployment_validation"},
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    # Same typed-conflict body shape as the actual deploy refusal above —
    # admission preview and deploy share one preflight and must not diverge.
    assert set(detail) == {"outcome", "receipt_id", "recorded_at_ms", "message", "why", "next_action", "admission"}
    assert detail["outcome"] == "conflict"
    assert detail["receipt_id"] is None
    assert detail["recorded_at_ms"] > 0
    assert "not currently selectable" in detail["message"]
    assert detail["admission"] is None


@pytest.mark.asyncio
async def test_deploy_reports_not_eligible_when_every_strategy_is_blocked(
    deploy_app: tuple[FastAPI, _FakeDeployRegistry],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast_app, _registry = deploy_app
    monkeypatch.setattr(
        panel_data_source,
        "load_strategy_validation_entries",
        lambda _registry: [_synthetic_blocked_entry("deployment_validation")],
    )

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy")

    body = response.json()
    assert [row["strategy_key"] for row in body["strategies"]] == ["deployment_validation"]
    assert body["strategies"][0]["selectable"] is False
    assert body["eligibility"]["eligible"] is False
    assert body["eligibility"]["reason_code"] == "STRATEGY_NOT_ACCEPTED_FOR_DEPLOY"
    strategy_gate = next(
        check for check in body["readiness_checks"] if check["gate_id"] == "strategy.validation_accepted"
    )
    assert strategy_gate["ready"] is False


@pytest.mark.asyncio
async def test_deploy_names_the_blocked_strategy_reason_even_when_every_strategy_is_blocked(
    deploy_app: tuple[FastAPI, _FakeDeployRegistry],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request naming the blocked strategy sees its own reason, not the catalog's generic headline.

    With every strategy blocked, ``view.eligibility`` reports the generic
    "nothing is selectable" headline. A request that names the blocked
    strategy must still see that strategy's own proof-naming reason —
    ``_require_alpaca_deploy_request`` checks strategy identity before
    falling back to the account-level eligibility gate.
    """
    fast_app, registry = deploy_app
    blocked = _synthetic_blocked_entry("deployment_validation")
    monkeypatch.setattr(
        panel_data_source,
        "load_strategy_validation_entries",
        lambda _registry: [blocked],
    )

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json={**_BODY, "strategy_key": "deployment_validation"},
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["message"] == "The selected strategy is not currently selectable for deployment."
    assert detail["why"]
    assert "no longer matches its recorded hash" in detail["why"]
    assert registry.deploy_calls == []
