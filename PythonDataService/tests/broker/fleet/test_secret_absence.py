"""Secret material appears in no row, payload or log — asserted, not assumed.

The spine never sees a credential (resolution is a Phase 2 code-owned
mapping), so this pins the *negative space*: with realistic secrets in the
environment and a full ceremony driven end to end, nothing about them —
value, fragment, length, or variable name — reaches the registry bytes, the
directory payload, or the logs. The worker key is durable registry identity,
so it *is* stored, but it never crosses the public projection.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.broker.fleet.records import RoutingReceiptState
from app.broker.fleet.store import registry_database_path
from tests.broker.fleet.conftest import provision_lane

FAKE_ALPHA_KEY = "PKZZFLEETKEYIDZZ0001"
FAKE_ALPHA_SECRET = "sk-zzz-fleet-secret-zzz-4e9b1d20"
CREDENTIAL_VARIABLE_NAMES = ("FAKE_ALPHA_API_KEY_ID", "FAKE_ALPHA_API_SECRET_KEY")
RELEASE_PROOF = "old-clerk-offline-and-obligations-clear"


@pytest.fixture
def seeded_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put realistic-looking fake credentials in the environment."""
    monkeypatch.setenv("FAKE_ALPHA_API_KEY_ID", FAKE_ALPHA_KEY)
    monkeypatch.setenv("FAKE_ALPHA_API_SECRET_KEY", FAKE_ALPHA_SECRET)


def _drive_ceremonies(fleet_service, tmp_path: Path) -> dict[str, object]:
    """Drive every ceremony once and return the directory payload."""
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="secrets-check", tmp_path=tmp_path
    )
    fleet_service.register_agent_session(clerk_id=lane.clerk_id, worker_key=lane.worker_key)
    fleet_service.reserve_assignment(
        broker="fake_alpha", clerk_id=lane.clerk_id, external_account_id="acct-secret"
    )
    fleet_service.confirm_assignment(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        external_account_id="acct-secret",
        binding_generation=1,
    )
    fleet_service.record_routing_receipt(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        operation_kind="bot_action",
        nonsecret_target_ref="strategy/sid-secret",
        idempotency_key="idem-secret",
        state=RoutingReceiptState.DELIVERED,
        upstream_receipt_ref="upstream/r-1",
    )
    secret_assignment = fleet_service._store.read_assignment(
        broker="fake_alpha", canonical_account_id="ACCT-SECRET"
    )
    assert secret_assignment is not None
    fleet_service.release_assignment(
        broker="fake_alpha",
        external_account_id="acct-secret",
        expected_assignment_generation=secret_assignment.assignment_generation,
        proof=RELEASE_PROOF,
    )
    return fleet_service.directory(include_retired=True)


def test_no_secret_reaches_a_row_a_payload_or_a_log(
    control_dir: Path,
    fleet_service,
    seeded_env,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Env secrets never reach the registry bytes, payloads or logs."""
    with caplog.at_level(logging.DEBUG):
        payload = _drive_ceremonies(fleet_service, control_dir.parent)

    registry_bytes = registry_database_path(control_dir).read_bytes()
    for wal_suffix in ("-wal", "-shm"):
        wal = registry_database_path(control_dir).with_name(
            registry_database_path(control_dir).name + wal_suffix
        )
        if wal.exists():
            registry_bytes += wal.read_bytes()
    rendered_payload = repr(payload)
    logged = "\n".join(
        record.getMessage() + str(record.__dict__) for record in caplog.records
    )

    for secret in (FAKE_ALPHA_KEY, FAKE_ALPHA_SECRET):
        assert secret.encode("utf-8") not in registry_bytes
        assert secret not in rendered_payload
        assert secret not in logged
    for variable in CREDENTIAL_VARIABLE_NAMES:
        assert variable.encode("utf-8") not in registry_bytes
        assert variable not in rendered_payload


def test_the_worker_key_is_stored_but_never_projected(
    control_dir: Path, fleet_service, seeded_env
) -> None:
    """The worker key lives only in the registry, never in a projection."""
    payload = _drive_ceremonies(fleet_service, control_dir.parent)
    db_path = registry_database_path(control_dir)
    registry_bytes = db_path.read_bytes()
    for wal_suffix in ("-wal", "-shm"):
        wal = db_path.with_name(db_path.name + wal_suffix)
        if wal.exists():
            registry_bytes += wal.read_bytes()
    # The durable identity is in the registry and only there.
    entries = payload["clerks"]
    assert entries, "the ceremony provisioned at least one clerk"
    for entry in entries:
        rendered_entry = repr(entry)
        assert "worker_key" not in rendered_entry
        assert "wkrk_" not in rendered_entry
    assert b"wkrk_" in registry_bytes
