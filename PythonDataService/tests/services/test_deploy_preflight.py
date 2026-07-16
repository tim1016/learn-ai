from __future__ import annotations

from pathlib import Path

from app.engine.live.account_artifacts import (
    AccountClerkLease,
    advance_account_clerk_generation,
    write_account_clerk_lease,
)
from app.engine.live.account_observation_lease import AccountObservationLeaseRepo
from app.services import deploy_preflight
from app.services.deploy_preflight import DeployPreflightSignals, author_deploy_blockers

ACCOUNT_ID = "DU1234567"
NOW_MS = 1_780_000_000_000


def _healthy() -> DeployPreflightSignals:
    return DeployPreflightSignals(
        daemon_reachable=True,
        broker_connection_state="connected",
        account_frozen=False,
        account_proven=True,
        fleet_blocks_starts=False,
        strategy_deployable=True,
        instance_already_running=False,
    )


def test_healthy_signals_produce_no_blockers() -> None:
    assert author_deploy_blockers(_healthy()) == []


def test_deploy_proof_switch_fails_closed_without_observation_lease(tmp_path: Path) -> None:
    assert not deploy_preflight._account_proof_is_current(
        authority="observation_lease",
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        account_truth=None,
        now_ms=NOW_MS,
    )


def test_deploy_proof_switch_accepts_verified_clerk_keyed_lease(tmp_path: Path) -> None:
    clerk = advance_account_clerk_generation(
        tmp_path,
        ACCOUNT_ID,
        phase="accepting",
        recorded_at_ms=NOW_MS,
        source="test",
    )
    write_account_clerk_lease(
        tmp_path,
        AccountClerkLease(
            account_id=ACCOUNT_ID,
            generation=clerk.generation,
            pid=123,
            ibkr_client_id=51,
            status="RUNNING",
            started_at_ms=NOW_MS,
            renewed_at_ms=NOW_MS,
            valid_until_ms=NOW_MS + 60_000,
        ),
    )
    AccountObservationLeaseRepo(tmp_path).renew(
        account_id=ACCOUNT_ID,
        observed_at_ms=NOW_MS,
        now_ms=NOW_MS,
        clerk_generation=clerk.generation,
    )

    assert deploy_preflight._account_proof_is_current(
        authority="observation_lease",
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        account_truth=None,
        now_ms=NOW_MS,
    )


def test_daemon_down_is_blocking_fix_elsewhere() -> None:
    blockers = author_deploy_blockers(_healthy().model_copy(update={"daemon_reachable": False}))
    ids = {blocker.condition.id: blocker for blocker in blockers}

    assert "daemon_down" in ids
    assert ids["daemon_down"].condition.severity == "blocking"
    assert ids["daemon_down"].disposition == "fix_elsewhere"
    assert ids["daemon_down"].primary_move is not None


def test_broker_disconnected_blocks_deploy() -> None:
    blockers = author_deploy_blockers(
        _healthy().model_copy(update={"broker_connection_state": "disconnected"})
    )

    assert "broker_disconnected" in {blocker.condition.id for blocker in blockers}


def test_broker_disconnected_blocker_contract() -> None:
    blocker = next(
        blocker
        for blocker in author_deploy_blockers(
            _healthy().model_copy(update={"broker_connection_state": "disconnected"})
        )
        if blocker.condition.id == "broker_disconnected"
    )

    assert blocker.headline == "Broker disconnected"
    assert blocker.detail == "Connect the IBKR session before deploying or starting this bot."
    assert blocker.primary_move is not None
    assert blocker.primary_move.label == "Connect the broker"
    assert blocker.primary_move.action.kind == "navigate"


def test_broker_soft_lost_is_wait_with_no_move() -> None:
    blockers = author_deploy_blockers(
        _healthy().model_copy(update={"broker_connection_state": "soft_lost"})
    )
    match = next(blocker for blocker in blockers if blocker.condition.id == "broker_soft_lost")

    assert match.condition.severity == "blocking"
    assert match.disposition == "wait"
    assert match.primary_move is None


def test_degraded_data_farm_is_blocking_wait() -> None:
    blockers = author_deploy_blockers(
        _healthy().model_copy(update={"broker_connection_state": "degraded_data_farm"})
    )
    match = next(blocker for blocker in blockers if blocker.condition.id == "broker_data_farm_degraded")

    assert match.condition.severity == "blocking"
    assert match.disposition == "wait"


def test_account_frozen_blocks_deploy() -> None:
    blockers = author_deploy_blockers(_healthy().model_copy(update={"account_frozen": True}))

    assert "account_frozen" in {blocker.condition.id for blocker in blockers}


def test_account_not_proven_blocks_deploy() -> None:
    blockers = author_deploy_blockers(_healthy().model_copy(update={"account_proven": False}))

    assert "account_not_proven" in {blocker.condition.id for blocker in blockers}


def test_fleet_contamination_blocks_deploy() -> None:
    blockers = author_deploy_blockers(_healthy().model_copy(update={"fleet_blocks_starts": True}))

    blocker = next(blocker for blocker in blockers if blocker.condition.id == "fleet_contaminated")

    assert blocker.primary_move is not None
    assert blocker.primary_move.action.route == "/broker/account-monitor"
    assert blocker.applies_to == "both"


def test_strategy_not_validated_blocks_deploy() -> None:
    blockers = author_deploy_blockers(_healthy().model_copy(update={"strategy_deployable": False}))

    assert "strategy_not_validated" in {blocker.condition.id for blocker in blockers}


def test_instance_already_running_blocks_deploy() -> None:
    blockers = author_deploy_blockers(_healthy().model_copy(update={"instance_already_running": True}))

    assert "instance_already_running" in {blocker.condition.id for blocker in blockers}


def test_every_blocker_satisfies_pairing_invariant() -> None:
    unhealthy = DeployPreflightSignals(
        daemon_reachable=False,
        broker_connection_state="disconnected",
        account_frozen=True,
        account_proven=False,
        fleet_blocks_starts=True,
        strategy_deployable=False,
        instance_already_running=True,
    )

    blockers = author_deploy_blockers(unhealthy)

    assert len(blockers) >= 6
    for blocker in blockers:
        assert blocker.anchor.kind == "surface"
        assert blocker.anchor.subject_key is None
        assert blocker.audience == "operator"
        if blocker.disposition in ("fix_here", "fix_elsewhere"):
            assert blocker.primary_move is not None
        if blocker.disposition == "wait":
            assert blocker.primary_move is None
