"""The host ceremony CLI answers, refuses and exits with pinned codes.

These drive ``scripts.manage_broker_fleet.main`` in-process against tmp
directories: the ceremonies themselves are covered above; this pins the
operator surface — argument shape, JSON output, and the 0/1/2 exit-code
vocabulary the other operator CLIs share.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from app.broker.fleet_composition import production_provider_adapters
from scripts.manage_broker_fleet import main
from tests.broker.fleet.conftest import FrozenClock


def _argv(*args: str) -> list[str]:
    """Wrap CLI arguments for a main() invocation."""
    return list(args)


def test_init_provision_verify_show_and_retire_round_trip(
    tmp_path: Path, capsys
) -> None:
    """The full ceremony round trip answers with pinned exit codes and JSON shapes.

    Since delivery A2 the CLI talks to the real composition registry, whose
    one production provider is Alpaca — this round trip exercises the actual
    production adapter, not an injected fake."""

    control_dir = tmp_path / "control"
    volume_root = tmp_path / "volumes" / "paper"
    volume_root.mkdir(parents=True)

    assert main(_argv("init", "--control-dir", str(control_dir))) == 0
    capsys.readouterr()

    assert (
        main(
            _argv(
                "provision",
                "--control-dir",
                str(control_dir),
                "--broker",
                "alpaca",
                "--label",
                "Paper research",
                "--volume-root",
                str(volume_root),
            )
        )
        == 0
    )
    provisioned = json.loads(capsys.readouterr().out)
    clerk_id = provisioned["clerk_id"]
    assert clerk_id.startswith("clrk_")
    assert provisioned["worker_key"].startswith("wkrk_")
    # The two transport tokens are minted once for the operator's environment
    # files and stored nowhere (audit 2026-09-13, finding 3).
    assert provisioned["agent_service_token"].startswith("svct_")
    assert provisioned["coordinator_service_token"].startswith("svct_")
    assert provisioned["agent_service_token"] != provisioned["coordinator_service_token"]

    # Endpoint approval is a host ceremony the registration later cites.
    assert (
        main(
            _argv(
                "approve-endpoint",
                "--control-dir",
                str(control_dir),
                "--clerk-id",
                clerk_id,
                "--endpoint-ref",
                "agent:paper-1",
                "--base-url",
                "http://alpaca-paper-clerk:8000/",
            )
        )
        == 0
    )
    approved = json.loads(capsys.readouterr().out)
    assert approved["endpoint_ref"] == "agent:paper-1"
    assert approved["base_url"] == "http://alpaca-paper-clerk:8000"

    # Rotation mints a fresh token without touching the registry.
    assert (
        main(
            _argv(
                "rotate-credentials",
                "--control-dir",
                str(control_dir),
                "--clerk-id",
                clerk_id,
                "--slot",
                "agent",
            )
        )
        == 0
    )
    rotated = json.loads(capsys.readouterr().out)
    assert rotated["service_token"].startswith("svct_")
    assert rotated["service_token"] != provisioned["agent_service_token"]

    assert (
        main(
            _argv(
                "verify",
                "--control-dir",
                str(control_dir),
                "--clerk-id",
                clerk_id,
                "--volume-root",
                str(volume_root),
            )
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["verified"] is True

    assert main(_argv("show", "--control-dir", str(control_dir))) == 0
    shown = json.loads(capsys.readouterr().out)
    assert [entry["clerk_id"] for entry in shown["clerks"]] == [clerk_id]
    assert "worker_key" not in shown["clerks"][0]

    assert (
        main(_argv("retire", "--control-dir", str(control_dir), "--clerk-id", clerk_id)) == 0
    )
    assert json.loads(capsys.readouterr().out)["lifecycle_state"] == "retired"


def test_the_ceremony_refusals_exit_two_and_the_unknown_provider_refuses(
    tmp_path: Path, capsys
) -> None:
    """Ceremony refusals exit 2 with their reason prefix; unknown providers fail closed."""
    control_dir = tmp_path / "control"
    volume_root = tmp_path / "volumes" / "x"
    volume_root.mkdir(parents=True)
    assert main(_argv("init", "--control-dir", str(control_dir))) == 0
    capsys.readouterr()

    # No production adapter is registered: provisioning any broker refuses.
    assert (
        main(
            _argv(
                "provision",
                "--control-dir",
                str(control_dir),
                "--broker",
                "tradier",
                "--label",
                "Tradier",
                "--volume-root",
                str(volume_root),
            )
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"].startswith("broker_not_supported:")

    assert main(_argv("retire", "--control-dir", str(control_dir), "--clerk-id", "clerk_missing")) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"].startswith("clerk_not_found:")


def test_release_requires_the_proof_token(tmp_path: Path, capsys) -> None:
    """Release demands the proof token and reports the released generation."""
    control_dir = tmp_path / "control"
    volume_root = tmp_path / "volumes" / "p"
    volume_root.mkdir(parents=True)
    assert main(_argv("init", "--control-dir", str(control_dir))) == 0
    assert (
        main(
            _argv(
                "provision",
                "--control-dir",
                str(control_dir),
                "--broker",
                "alpaca",
                "--label",
                "p",
                "--volume-root",
                str(volume_root),
            )
        )
        == 0
    )
    capsys.readouterr()

    clock = FrozenClock()
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    try:
        clerk_id = service._store.list_clerks()[0].clerk_id
        service.reserve_assignment(
            broker="alpaca", clerk_id=clerk_id, external_account_id="acct-cli"
        )
    finally:
        service.close()

    assert (
        main(
            _argv(
                "release-assignment",
                "--control-dir",
                str(control_dir),
                "--broker",
                "alpaca",
                "--account-id",
                "acct-cli",
                "--expected-generation",
                "1",
                "--proof",
                "not-the-proof",
            )
        )
        == 2
    )
    capsys.readouterr()
    assert (
        main(
            _argv(
                "release-assignment",
                "--control-dir",
                str(control_dir),
                "--broker",
                "alpaca",
                "--account-id",
                "acct-cli",
                "--expected-generation",
                "1",
                "--proof",
                "old-clerk-offline-and-obligations-clear",
            )
        )
        == 0
    )
    released = json.loads(capsys.readouterr().out)
    assert released["state"] == "released"
