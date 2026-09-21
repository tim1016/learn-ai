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
from app.utils.timestamps import now_ms_utc
from scripts.manage_broker_fleet import main
from tests.broker.fleet.conftest import FrozenClock, downgrade_backup_to_v2


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


def test_release_requires_the_drain_and_attribution_and_reports_the_generation(
    tmp_path: Path, capsys
) -> None:
    """Release demands the drained state and a bounded attribution, and
    reports the released generation (ADR 0063 Decision 4).

    The CLI's own service runs on the wall clock, so the drain is entered
    through a service seam whose frozen clock sits in the past — the stored
    deadline has already elapsed by the time the CLI runs."""
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

    # A provisioned predecessor refuses: the door must be closed first.
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
                "--operator",
                "cli-operator",
                "--change-ref",
                "cli-change-1",
            )
        )
        == 2
    )
    refusal = json.loads(capsys.readouterr().out)
    assert refusal["error"].startswith("clerk_drain_required:")

    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    try:
        service.drain_clerk(clerk_id=clerk_id)
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
                "--operator",
                "cli-operator",
                "--change-ref",
                "cli-change-1",
            )
        )
        == 0
    )
    released = json.loads(capsys.readouterr().out)
    assert released["state"] == "released"
    assert released["lane_confirmation"] == "absent"
    assert released["attested_operator"] == "cli-operator"


def test_drain_and_force_retire_answer_over_the_cli(tmp_path: Path, capsys) -> None:
    """The drain and force-retire verbs answer with the drain window's
    endpoints, refuse before the deadline (exit 2), and complete past it with
    the forced attribution the operator typed."""
    control_dir = tmp_path / "control"
    volume_root = tmp_path / "volumes" / "fr"
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
                "fr",
                "--volume-root",
                str(volume_root),
            )
        )
        == 0
    )
    clerk_id = json.loads(capsys.readouterr().out)["clerk_id"]

    assert (
        main(_argv("drain", "--control-dir", str(control_dir), "--clerk-id", clerk_id)) == 0
    )
    drained = json.loads(capsys.readouterr().out)
    assert drained["lifecycle_state"] == "draining"
    assert drained["draining_since_ms"] is not None
    assert drained["drain_deadline_at_ms"] is not None

    # The CLI's wall clock is inside the drain window the CLI itself just
    # opened: force-retire refuses and names the outstanding instant.
    assert (
        main(
            _argv(
                "force-retire",
                "--control-dir",
                str(control_dir),
                "--clerk-id",
                clerk_id,
                "--operator",
                "cli-operator",
                "--change-ref",
                "cli-change-2",
            )
        )
        == 2
    )
    refusal = json.loads(capsys.readouterr().out)
    assert refusal["error"].startswith("clerk_drain_deadline_pending:")

    # The completing path needs a deadline that has elapsed against the CLI's
    # wall clock; §7.3 forbids moving the first lane's, so enter a second
    # lane's drain through a service seam whose frozen clock sits far in the
    # past.
    past = FrozenClock(start_ms=now_ms_utc() - 90 * 86_400_000)
    stale_root = tmp_path / "volumes" / "stale"
    stale_root.mkdir(parents=True)
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=past,
    )
    stale_lane = None
    try:
        stale_lane = service.provision_clerk(
            broker="alpaca",
            display_label="stale",
            volume_root=stale_root,
            attestation_id="vol-stale",
        )
        service.register_agent_session(
            fleet_protocol_version=2,
            clerk_id=stale_lane.clerk.clerk_id,
            worker_key=stale_lane.clerk.worker_key,
        )
        service.drain_clerk(clerk_id=stale_lane.clerk.clerk_id)
    finally:
        service.close()

    assert stale_lane is not None
    assert (
        main(
            _argv(
                "force-retire",
                "--control-dir",
                str(control_dir),
                "--clerk-id",
                stale_lane.clerk.clerk_id,
                "--operator",
                "cli-operator",
                "--change-ref",
                "cli-change-3",
            )
        )
        == 0
    )
    retired = json.loads(capsys.readouterr().out)
    assert retired["lifecycle_state"] == "retired"
    assert retired["lane_confirmation"] == "absent"
    assert retired["retire_operator"] == "cli-operator"


def test_compatibility_cli_requires_complete_zero_hit_evidence_before_retirement(
    tmp_path: Path, capsys
) -> None:
    """The host command retains aliases by default and refuses an incomplete retirement proof."""
    # One anchor, explicit offsets: nothing in this test depends on wall-clock
    # milliseconds elapsing between two CLI invocations. anchor_ms stays a
    # live now_ms_utc() read, not a hardcoded or frozen value, because the
    # CLI computes evaluated_at internally (scripts/manage_broker_fleet.py
    # exposes no --evaluated-at-ms) and a stale anchor would age past the
    # limits within a day. The evidence-age and window bounds are
    # DEFAULT_MAX_EVIDENCE_AGE_MS (24h) / DEFAULT_MAX_WINDOW_DURATION_MS (7d)
    # in compatibility_retirement.py, so these offsets are inside every
    # limit while still ordering strictly.
    anchor_ms = now_ms_utc()
    window_start_ms = anchor_ms - 3_000
    window_end_ms = anchor_ms - 2_000
    evidence_ms = anchor_ms - 1_000
    aggregate = {"schema_version": 2, "updated_at_ms": window_start_ms, "route_hits": []}
    start_evidence = tmp_path / "start-evidence.json"
    end_evidence = tmp_path / "end-evidence.json"
    start_evidence.write_text(json.dumps(aggregate), encoding="utf-8")
    end_evidence.write_text(json.dumps(aggregate), encoding="utf-8")
    start_snapshot = tmp_path / "start.json"
    end_snapshot = tmp_path / "end.json"
    assert (
        main(
            _argv(
                "compatibility-snapshot",
                "--evidence-path",
                str(start_evidence),
                "--snapshot-path",
                str(start_snapshot),
                "--source-label",
                "paper",
            )
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            _argv(
                "compatibility-snapshot",
                "--evidence-path",
                str(end_evidence),
                "--snapshot-path",
                str(end_snapshot),
                "--source-label",
                "paper",
            )
        )
        == 0
    )
    capsys.readouterr()
    start_payload = json.loads(start_snapshot.read_text(encoding="utf-8"))
    end_payload = json.loads(end_snapshot.read_text(encoding="utf-8"))
    start_payload["captured_at_ms"] = window_start_ms
    end_payload["captured_at_ms"] = window_end_ms
    start_snapshot.write_text(json.dumps(start_payload), encoding="utf-8")
    end_snapshot.write_text(json.dumps(end_payload), encoding="utf-8")
    inventory = tmp_path / "inventory.json"
    scoped = tmp_path / "scoped.json"
    operator = tmp_path / "operator.json"
    inventory.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "complete": True,
                "generated_at_ms": evidence_ms,
                "consumers": [
                    {
                        "consumer": "alpaca-desk",
                        "attestation": "all-retained-reads-scoped",
                        "route_families": [
                            "broker_bots",
                            "broker_configuration",
                            "broker_v2_panel",
                            "brokers_lane_extras",
                            "run_replay",
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    scoped.write_text(
        json.dumps(
            {"schema_version": 1, "observed_at_ms": evidence_ms, "unresolved_scoped_route_failures": 0}
        ),
        encoding="utf-8",
    )
    operator.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "receipt_id": "operator-acceptance-1",
                "operator": "fleet-owner",
                "issued_at_ms": evidence_ms,
                "representative_window": True,
                "retire_compatibility_reads": True,
            }
        ),
        encoding="utf-8",
    )
    decision = tmp_path / "decision.json"
    rollout = tmp_path / "retirement-rollout.json"
    route_state = tmp_path / "compatibility" / "route_state.json"
    common = _argv(
        "--start-snapshot",
        str(start_snapshot),
        "--end-snapshot",
        str(end_snapshot),
        "--consumer-inventory",
        str(inventory),
        "--scoped-route-evidence",
        str(scoped),
        "--operator-receipt",
        str(operator),
        "--decision-receipt-path",
        str(decision),
    )
    assert main(["compatibility-evaluate", *common]) == 0
    evaluated = json.loads(capsys.readouterr().out)
    assert evaluated["state"] == "measurement"
    assert decision.exists()

    assert (
        main(
            [
                "compatibility-retire",
                *common,
                "--route-state-path",
                str(route_state),
                "--retirement-rollout-path",
                str(rollout),
            ]
        )
        == 0
    )
    retired = json.loads(capsys.readouterr().out)
    assert retired["state"] == "retired"
    assert retired["rollout_state"] == "complete"
    assert json.loads(route_state.read_text(encoding="utf-8"))["state"] == "retired"

    inventory.write_text(json.dumps({"schema_version": 1, "complete": False}), encoding="utf-8")
    assert (
        main(
            [
                "compatibility-retire",
                *common,
                "--route-state-path",
                str(tmp_path / "bad.json"),
                "--retirement-rollout-path",
                str(tmp_path / "bad-rollout.json"),
            ]
        )
        == 2
    )
    assert "compatibility_retirement_refused:" in json.loads(capsys.readouterr().out)["error"]


def test_backup_restore_and_d_rollback_cli_enter_the_reconciliation_hold(
    tmp_path: Path, capsys
) -> None:
    """The operator surface never restores a registry into immediately routable state."""
    control_dir = tmp_path / "control"
    volume_root = tmp_path / "volumes" / "paper"
    volume_root.mkdir(parents=True)
    backup_dir = tmp_path / "backup"
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
                "Paper",
                "--volume-root",
                str(volume_root),
            )
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["clerk_id"].startswith("clrk_")
    assert (
        main(
            _argv(
                "backup-registry",
                "--control-dir",
                str(control_dir),
                "--backup-dir",
                str(backup_dir),
            )
        )
        == 0
    )
    backup = json.loads(capsys.readouterr().out)
    assert backup["active_clerk_ids"] == []
    # #2073b: this build writes schema v3, whose nested-root fence a D-era
    # binary cannot enforce, so its own backup is not D-rollback evidence.
    assert (
        main(
            _argv(
                "rollback-d-compatible",
                "--control-dir",
                str(control_dir),
                "--backup-dir",
                str(backup_dir),
            )
        )
        == 2
    )
    assert "newer" in json.loads(capsys.readouterr().out)["error"]
    assert (
        main(
            _argv(
                "restore-registry",
                "--control-dir",
                str(control_dir),
                "--backup-dir",
                str(backup_dir),
            )
        )
        == 0
    )
    restored = json.loads(capsys.readouterr().out)
    assert restored["mutations_closed"] is True
    # One hold, reported once: the CLI never claims two independent fences
    # over a single RegistryRecoveryState bit.
    assert "assignment_mutation_closed" not in restored
    assert restored["rollback_topology"] == "current"
    # A D rollback needs pre-upgrade evidence, so produce it from the v3 backup.
    downgrade_backup_to_v2(backup_dir)
    assert (
        main(
            _argv(
                "rollback-d-compatible",
                "--control-dir",
                str(control_dir),
                "--backup-dir",
                str(backup_dir),
            )
        )
        == 0
    )
    rolled_back = json.loads(capsys.readouterr().out)
    assert rolled_back["mutations_closed"] is True
    assert "assignment_mutation_closed" not in rolled_back
    assert rolled_back["rollback_topology"] == "d_compatible"
    assert (
        main(
            _argv(
                "closeout-empty-registry",
                "--control-dir",
                str(control_dir),
                "--operator",
                "fleet-owner",
                "--change-ref",
                "incident-2049",
            )
        )
        == 0
    )
    closeout = json.loads(capsys.readouterr().out)
    assert closeout["mutations_closed"] is False
    assert "assignment_mutation_closed" not in closeout
    assert closeout["empty_inventory_attestation"]["operator"] == "fleet-owner"
