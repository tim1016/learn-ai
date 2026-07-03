"""Tests for the broker Account Truth router."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.ibkr.models import IbkrConnectionHealth
from app.engine.live.account_artifacts import AccountFreezeEvidence, write_account_freeze
from app.routers import broker_account_truth


def _health() -> IbkrConnectionHealth:
    return IbkrConnectionHealth(
        mode="paper",
        host="127.0.0.1",
        port=4002,
        client_id=7,
        connected=True,
        account_id="DU1234567",
        is_paper=True,
        fetched_at_ms=1_780_000_000_000,
        connection_state="connected",
        last_transition_ms=1_780_000_000_000,
    )


def _client() -> SimpleNamespace:
    return SimpleNamespace(health=_health)


def _patch_endpoint_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    live_runs_root = tmp_path / "live_runs"
    live_runs_root.mkdir(exist_ok=True)
    monkeypatch.setattr(
        broker_account_truth,
        "get_settings",
        lambda: SimpleNamespace(live_runs_root=str(live_runs_root)),
    )
    monkeypatch.setattr(broker_account_truth, "get_monitor", lambda: None)


@pytest.mark.asyncio
async def test_account_truth_endpoint_passes_active_account_freeze_to_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_endpoint_dependencies(monkeypatch, tmp_path)
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU1234567",
            reason="restart_intensity.threshold_breached",
            source="account_restart_intensity",
            recorded_at_ms=1_780_000_002_000,
            operator_next_step="STOP_RESTARTING_AND_RECOVER_ACCOUNT",
        ),
    )
    captured: dict[str, object] = {}

    async def fake_refresh_account_truth_and_update_cache(*_, **kwargs) -> SimpleNamespace:
        captured["collection_context"] = kwargs["collection_context"]
        return SimpleNamespace()

    monkeypatch.setattr(
        broker_account_truth,
        "refresh_account_truth_and_update_cache",
        fake_refresh_account_truth_and_update_cache,
    )

    await broker_account_truth.account_truth_endpoint(_client())

    collection_context = captured["collection_context"]
    assert collection_context.account_recovery_state.status == "frozen"
    assert collection_context.evidence_gaps == ()


@pytest.mark.asyncio
async def test_account_truth_endpoint_adds_gap_when_freeze_state_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_endpoint_dependencies(monkeypatch, tmp_path)
    account_root = tmp_path / "accounts" / "DU1234567"
    account_root.mkdir(parents=True)
    (account_root / "unresolved_exposure.flag").write_text("{not-json", encoding="utf-8")
    captured: dict[str, object] = {}

    async def fake_refresh_account_truth_and_update_cache(*_, **kwargs) -> SimpleNamespace:
        captured["collection_context"] = kwargs["collection_context"]
        return SimpleNamespace()

    monkeypatch.setattr(
        broker_account_truth,
        "refresh_account_truth_and_update_cache",
        fake_refresh_account_truth_and_update_cache,
    )

    await broker_account_truth.account_truth_endpoint(_client())

    collection_context = captured["collection_context"]
    evidence_gaps = collection_context.evidence_gaps
    assert collection_context.account_recovery_state.status == "unreadable"
    assert len(evidence_gaps) == 1
    gap = evidence_gaps[0]
    assert gap.source == "account_freeze"
    assert gap.severity == "critical"
    assert "Account freeze state unavailable" in gap.message
