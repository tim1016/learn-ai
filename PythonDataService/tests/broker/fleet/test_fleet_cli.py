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
from scripts.manage_broker_fleet import main
from tests.broker.fleet.conftest import FakeProviderAdapter, FrozenClock, fake_alpha


def _argv(*args: str) -> list[str]:
    """Wrap CLI arguments for a main() invocation."""
    return list(args)


def test_init_provision_verify_show_and_retire_round_trip(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    # The CLI talks to the production adapter set; a test adapter is injected
    # by patching the production mapping — proving injection exists at the
    # seam without the fake ever being *registered* in code.
    """The full ceremony round trip answers with pinned exit codes and JSON shapes."""
    monkeypatch.setattr(
        "app.broker.fleet.service.PRODUCTION_PROVIDER_ADAPTERS",
        {"fake_alpha": fake_alpha()},
    )
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
                "fake_alpha",
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


def test_release_requires_the_proof_token(tmp_path: Path, capsys, monkeypatch) -> None:
    """Release demands the proof token and reports the released generation."""
    monkeypatch.setattr(
        "app.broker.fleet.service.PRODUCTION_PROVIDER_ADAPTERS",
        {"fake_alpha": fake_alpha()},
    )
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
                "fake_alpha",
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
        provider_adapters={"fake_alpha": FakeProviderAdapter("fake_alpha")},
        clock=clock,
    )
    try:
        clerk_id = service._store.list_clerks()[0].clerk_id
        service.reserve_assignment(
            broker="fake_alpha", clerk_id=clerk_id, external_account_id="acct-cli"
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
                "fake_alpha",
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
                "fake_alpha",
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
