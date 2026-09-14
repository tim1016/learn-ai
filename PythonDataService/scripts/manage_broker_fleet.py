"""The host ceremony surface for the broker clerk fleet (ADR 0062, PRD §9.4).

Enrollment, retirement, verification and release run here — on the host,
against the coordinator's control volume — because each requires host powers
a browser-gated control plane must not hold: creating and mounting named
volumes, injecting environment-only credentials, and proving agents offline.
ADR 0062 Decision 8 records why these are not browser actions in this
delivery and designs this CLI as the ceremony surface a future authenticated
UI slice may wrap.

Typical use, inside the data-plane image against the coordinator control
volume::

    python -m scripts.manage_broker_fleet init --control-dir /app/artifacts/fleet

    python -m scripts.manage_broker_fleet provision \\
        --control-dir /app/artifacts/fleet --broker alpaca \\
        --label "Paper research" \\
        --volume-root /app/artifacts/clerks/paper \\
        --attestation-id learn-ai-alpaca-paper-clerk

    python -m scripts.manage_broker_fleet verify \\
        --control-dir /app/artifacts/fleet --clerk-id clrk_... \\
        --volume-root /app/artifacts/clerks/paper

The worker key printed by ``provision`` is the one secret-shaped artifact the
ceremony mints; it is handed to exactly one agent process and never returned
over any API.

Exit codes: ``0`` the command answered; ``1`` it could not be run as asked;
``2`` the ceremony refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.broker.fleet.compatibility_retirement import (
    CompatibilityRetirementRefusal,
    CompatibilityRouteState,
    capture_snapshot,
    evaluate_retirement,
    write_retirement_receipt,
    write_route_state,
)
from app.broker.fleet.errors import (
    ClerkVolumeCloneDetected,
    FleetControlError,
    FleetRegistryUnavailable,
)
from app.broker.fleet.identity import new_service_token
from app.broker.fleet.recovery import (
    D_COMPATIBLE_SCHEMA_VERSION,
    closeout_empty_registry_recovery,
    create_registry_backup,
    read_recovery_state,
    reconcile_restored_lane,
    restore_registry_backup,
)
from app.broker.fleet.schema import SCHEMA_VERSION
from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from app.broker.fleet_composition import production_provider_adapters
from scripts._operator_cli import jsonable


def _write(payload: object) -> None:
    # stdout.write, not print: the sibling operator CLIs emit machine-readable
    # JSON lines and the repo's no-print rule binds here too.
    """Emit one machine-readable JSON line to stdout."""
    sys.stdout.write(json.dumps(jsonable(payload), sort_keys=True) + "\n")


def _service(args: argparse.Namespace) -> FleetControlService:
    """Open the registry and build the control service for a command."""
    store = FleetRegistryStore.open(control_dir=Path(args.control_dir))
    return FleetControlService(
        store=store,
        provider_adapters=production_provider_adapters(),
    )


def _init(args: argparse.Namespace) -> int:
    """Handle ``init``: create or open the registry."""
    store = FleetRegistryStore.open(control_dir=Path(args.control_dir))
    try:
        _write(
            {
                "control_dir": args.control_dir,
                "registry_id": store.registry_id,
                "schema_version": store.schema_version,
            }
        )
    finally:
        store.close()
    return 0


def _provision(args: argparse.Namespace) -> int:
    """Handle ``provision``: mint one clerk lane on a fresh volume."""
    service = _service(args)
    try:
        provisioned = service.provision_clerk(
            broker=args.broker,
            display_label=args.label,
            volume_root=Path(args.volume_root),
            attestation_id=args.attestation_id,
            deployment_namespace=args.deployment_namespace,
        )
        _write(
            {
                "clerk_id": provisioned.clerk.clerk_id,
                "broker": provisioned.clerk.broker,
                "volume_id": provisioned.clerk.volume_id,
                "deployment_namespace": provisioned.clerk.deployment_namespace,
                "worker_key": provisioned.clerk.worker_key,
                "agent_service_token": provisioned.agent_service_token,
                "coordinator_service_token": provisioned.coordinator_service_token,
                "attestation": (
                    provisioned.clerk.volume_attestation_kind,
                    provisioned.clerk.volume_attestation_id,
                ),
                "note": "Persist the worker key and the two service tokens in the "
                "operator's uncommitted environment files now: the tokens are "
                "minted once, stored nowhere, and re-running never reprints them.",
            }
        )
    finally:
        service.close()
    return 0


def _rotate_credentials(args: argparse.Namespace) -> int:
    """Handle ``rotate-credentials``: mint one fresh transport token."""
    service = _service(args)
    try:
        descriptor = service.describe_clerk(args.clerk_id)
        token = new_service_token()
        _write(
            {
                "clerk_id": args.clerk_id,
                "broker": descriptor.broker,
                "slot": args.slot,
                "service_token": token,
                "note": "The registry stores no credential; copy this token into the "
                "operator's uncommitted environment file for the serving side and "
                "restart both processes.",
            }
        )
    finally:
        service.close()
    return 0


def _approve_endpoint(args: argparse.Namespace) -> int:
    """Handle ``approve-endpoint``: approve or re-target an agent destination."""
    service = _service(args)
    try:
        record = service.approve_endpoint(
            clerk_id=args.clerk_id,
            endpoint_ref=args.endpoint_ref,
            base_url=args.base_url,
        )
        _write(
            {
                "clerk_id": record.clerk_id,
                "endpoint_ref": record.endpoint_ref,
                "base_url": record.base_url,
                "updated_at_ms": record.updated_at_ms,
            }
        )
    finally:
        service.close()
    return 0


def _verify(args: argparse.Namespace) -> int:
    """Handle ``verify``: re-run the volume identity gate."""
    service = _service(args)
    try:
        marker = service.verify_clerk_volume(
            clerk_id=args.clerk_id, volume_root=Path(args.volume_root)
        )
        _write({"clerk_id": args.clerk_id, "verified": True, "marker_version": marker.marker_version})
    finally:
        service.close()
    return 0


def _show(args: argparse.Namespace) -> int:
    """Handle ``show``: print the directory or one clerk."""
    service = _service(args)
    try:
        if args.clerk_id:
            _write(service.describe_clerk(args.clerk_id))
        else:
            _write(service.directory(include_retired=args.include_retired))
    finally:
        service.close()
    return 0


def _retire(args: argparse.Namespace) -> int:
    """Handle ``retire``: retire a clerk terminally."""
    service = _service(args)
    try:
        retired = service.retire_clerk(clerk_id=args.clerk_id)
        _write({"clerk_id": retired.clerk_id, "lifecycle_state": str(retired.lifecycle_state)})
    finally:
        service.close()
    return 0


def _release(args: argparse.Namespace) -> int:
    """Handle ``release-assignment``: run the host release ceremony."""
    service = _service(args)
    try:
        released = service.release_assignment(
            broker=args.broker,
            external_account_id=args.account_id,
            expected_assignment_generation=args.expected_generation,
            proof=args.proof,
        )
        _write(
            {
                "broker": released.broker,
                "canonical_external_account_id": released.canonical_external_account_id,
                "state": str(released.state),
                "assignment_generation": released.assignment_generation,
            }
        )
    finally:
        service.close()
    return 0


def _compatibility_snapshot(args: argparse.Namespace) -> int:
    """Capture one privacy-preserving compatibility aggregate snapshot."""
    snapshot = capture_snapshot(
        evidence_path=Path(args.evidence_path),
        snapshot_path=Path(args.snapshot_path),
        source_label=args.source_label,
    )
    _write(snapshot)
    return 0


def _compatibility_evaluate(args: argparse.Namespace) -> int:
    """Evaluate retirement evidence but leave all aliases in measurement mode."""
    receipt = _evaluate_compatibility(args)
    _write_compatibility_receipt(args, receipt)
    _write({"state": CompatibilityRouteState.MEASUREMENT.value, "receipt": receipt})
    return 0


def _compatibility_retire(args: argparse.Namespace) -> int:
    """Retire only the retained unscoped reads after an eligible evaluation."""
    receipt = _evaluate_compatibility(args)
    _write_compatibility_receipt(args, receipt)
    state_paths = [Path(path) for path in args.route_state_path]
    if not state_paths:
        raise CompatibilityRetirementRefusal(
            "Retirement needs at least one lane-local compatibility route state path."
        )
    if len(set(state_paths)) != len(state_paths):
        raise CompatibilityRetirementRefusal("Retirement route state paths must be unique.")
    states = [
        write_route_state(
            state_path=state_path,
            state=CompatibilityRouteState.RETIRED,
            retirement_receipt=receipt,
        )
        for state_path in state_paths
    ]
    _write(
        {
            "state": CompatibilityRouteState.RETIRED.value,
            "operator_receipt_id": receipt["operator_receipt_id"],
            "route_state_count": len(states),
        }
    )
    return 0


def _reassign(args: argparse.Namespace) -> int:
    """Handle ``reassign-assignment``: verify then transfer one ownership fence."""
    service = _service(args)
    try:
        assigned = service.reassign_assignment(
            broker=args.broker,
            external_account_id=args.account_id,
            expected_assignment_generation=args.expected_generation,
            proof=args.proof,
            successor_clerk_id=args.successor_clerk_id,
            successor_volume_root=Path(args.successor_volume_root),
        )
        _write(
            {
                "broker": assigned.broker,
                "canonical_external_account_id": assigned.canonical_external_account_id,
                "clerk_id": assigned.clerk_id,
                "assignment_generation": assigned.assignment_generation,
                "state": str(assigned.state),
                "routing_open": False,
                "note": "The successor remains unroutable until its own agent "
                "confirms the exact binding and existing provider gates admit it.",
            }
        )
    finally:
        service.close()
    return 0


def _backup_registry(args: argparse.Namespace) -> int:
    """Handle ``backup-registry``: snapshot nonsecret coordinator evidence."""
    service = _service(args)
    try:
        manifest = create_registry_backup(service._store, backup_dir=Path(args.backup_dir))
        _write(
            {
                "registry_id": manifest.registry_id,
                "schema_version": manifest.registry_schema_version,
                "database_sha256": manifest.database_sha256,
                "active_clerk_ids": manifest.active_clerk_ids,
                "backup_dir": args.backup_dir,
            }
        )
    finally:
        service.close()
    return 0


def _restore_registry(args: argparse.Namespace) -> int:
    """Restore a registry without ever opening a blank replacement registry."""
    manifest = restore_registry_backup(
        control_dir=Path(args.control_dir),
        backup_dir=Path(args.backup_dir),
        max_schema_version=D_COMPATIBLE_SCHEMA_VERSION if args.d_compatible else SCHEMA_VERSION,
    )
    state = read_recovery_state(Path(args.control_dir))
    assert state is not None
    _write(
        {
            "registry_id": manifest.registry_id,
            "schema_version": manifest.registry_schema_version,
            "required_clerk_ids": manifest.active_clerk_ids,
            "routing_closed": state.routing_closed,
            "assignment_mutation_closed": state.routing_closed,
            "rollback_topology": "d_compatible" if args.d_compatible else "current",
        }
    )
    return 0


def _evaluate_compatibility(args: argparse.Namespace) -> dict[str, object]:
    """Build the shared fail-closed compatibility retirement receipt."""
    return evaluate_retirement(
        start_snapshot_paths=[Path(path) for path in args.start_snapshot],
        end_snapshot_paths=[Path(path) for path in args.end_snapshot],
        consumer_inventory_path=Path(args.consumer_inventory),
        scoped_route_evidence_path=Path(args.scoped_route_evidence),
        operator_receipt_path=Path(args.operator_receipt),
        max_evidence_age_ms=args.max_evidence_age_ms,
        max_window_duration_ms=args.max_window_duration_ms,
    )


def _write_compatibility_receipt(args: argparse.Namespace, receipt: dict[str, object]) -> None:
    """Write the durable restricted-record receipt before changing route state."""
    write_retirement_receipt(Path(args.decision_receipt_path), receipt)


def _reconcile_registry(args: argparse.Namespace) -> int:
    """Handle one lane of a restored-registry reconciliation ceremony."""
    try:
        provider_summary = json.loads(args.provider_summary)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--provider-summary must be JSON: {exc}") from exc
    if not isinstance(provider_summary, dict):
        raise ValueError("--provider-summary must be a JSON object")
    service = _service(args)
    try:
        state = reconcile_restored_lane(
            service,
            clerk_id=args.clerk_id,
            volume_root=Path(args.volume_root),
            provider_summary=provider_summary,
        )
        _write(
            {
                "registry_id": state.registry_id,
                "clerk_id": args.clerk_id,
                "reconciled_clerk_ids": state.reconciled_clerk_ids,
                "required_clerk_ids": state.required_clerk_ids,
                "routing_closed": state.routing_closed,
            }
        )
    finally:
        service.close()
    return 0


def _closeout_empty_registry(args: argparse.Namespace) -> int:
    """Close an empty-active-inventory restore with named host evidence."""
    store = FleetRegistryStore.open(control_dir=Path(args.control_dir))
    try:
        state = closeout_empty_registry_recovery(
            store,
            control_dir=Path(args.control_dir),
            operator=args.operator,
            change_ref=args.change_ref,
        )
        _write(
            {
                "registry_id": state.registry_id,
                "routing_closed": state.routing_closed,
                "assignment_mutation_closed": state.routing_closed,
                "empty_inventory_attestation": state.empty_inventory_attestation,
            }
        )
    finally:
        store.close()
    return 0


def _migrate_existing(args: argparse.Namespace) -> int:
    """Handle ``migrate-existing``: enrol the existing Alpaca lane, resumably.

    An offline ceremony (the agent must be stopped). Each step is idempotent
    and derives its resume point from the durable artifacts themselves — the
    registry row, the volume marker, the seeded binding generation, the
    imported assignment and the confirmation evidence — so an interrupted
    run converges on rerun and identities are never reminted to get past a
    partial failure (audit 2026-09-13, finding 10).
    """

    from app.broker_configuration.worker_lifecycle import installation_worker

    # The ceremony is offline by contract: holding the installation worker
    # lock both proves it and refuses to supersede a live agent's session.
    with installation_worker() as worker_refusal:
        if worker_refusal is not None:
            raise ClerkVolumeCloneDetected(
                "The installation worker lock is held — a clerk agent appears to "
                "be running on this volume; migrate-existing is an offline "
                "ceremony.",
                next_step="Stop the agent, then re-run the ceremony.",
            )
        return _migrate_existing_locked(args)


def _migrate_existing_locked(args: argparse.Namespace) -> int:
    """The ceremony body, run under the installation worker lock."""
    from app.broker.alpaca.clerk.fleet_adapter import AlpacaProviderAdapter
    from app.broker.fleet import volume as volume_module
    from app.broker.fleet.confirmation import (
        ConfirmationEvidence,
        read_confirmation_evidence,
        write_confirmation_evidence,
    )
    from app.utils.timestamps import now_ms_utc

    service = _service(args)
    volume_root = Path(args.volume_root)
    try:
        attestation_id = args.attestation_id or volume_root.name
        marker = volume_module.read_volume_marker(volume_root)
        if marker is None:
            # Resume rule: a registry row for this attestation without a
            # marker is a partially completed run. Identities are reused,
            # never reminted — the marker is written for the registered row.
            resumed = service._store.find_clerk_by_attestation(
                deployment_namespace=args.deployment_namespace,
                attestation_kind="compose_named_volume",
                attestation_id=attestation_id,
            )
            if resumed is not None:
                if Path(resumed.volume_root) != volume_root.resolve():
                    raise ClerkVolumeCloneDetected(
                        f"A partial enrolment registered attestation "
                        f"{attestation_id!r} with root {resumed.volume_root}, not "
                        f"{volume_root}; an interrupted migration never remints "
                        "identities.",
                        next_step="Restore the registry from the control volume's "
                        "backup, or complete the enrolment on the original root.",
                    )
                marker = volume_module.write_volume_marker(
                    volume_root,
                    volume_module.VolumeMarker(
                        marker_version=volume_module.MARKER_SCHEMA_VERSION,
                        broker=resumed.broker,
                        clerk_id=resumed.clerk_id,
                        volume_id=resumed.volume_id,
                        attestation_kind=resumed.volume_attestation_kind,
                        attestation_id=resumed.volume_attestation_id,
                        created_at_ms=resumed.created_at_ms,
                    ),
                )
                _write({"step": "marker-resumed", "clerk_id": resumed.clerk_id})
                clerk_id = resumed.clerk_id
            else:
                provisioned = service.provision_clerk(
                    broker=args.broker,
                    display_label=args.label,
                    volume_root=volume_root,
                    attestation_id=args.attestation_id,
                    deployment_namespace=args.deployment_namespace,
                )
                _write(
                    {
                        "step": "identities-issued",
                        "clerk_id": provisioned.clerk.clerk_id,
                        "worker_key": provisioned.clerk.worker_key,
                        "agent_service_token": provisioned.agent_service_token,
                        "coordinator_service_token": provisioned.coordinator_service_token,
                        "note": "Persist these now in the operator's uncommitted "
                        "environment files; a resumed run never reprints them.",
                    }
                )
                clerk_id = provisioned.clerk.clerk_id
        else:
            clerk_id = marker.clerk_id
            if service._store.read_clerk(clerk_id) is None:
                raise ClerkVolumeCloneDetected(
                    f"The volume carries the marker of clerk {clerk_id}, which this "
                    "registry does not know; identities are never reminted over a "
                    "marked volume.",
                    next_step="Restore the registry from the control volume's backup "
                    "so it knows this clerk again.",
                )
            service.verify_clerk_volume(clerk_id=clerk_id, volume_root=volume_root)
        clerk_row = service._store.read_clerk(clerk_id)
        assert clerk_row is not None

        seeded, generation = _seed_effective_binding_generation(volume_root)
        account, profile_id, revision = _effective_tuple(volume_root)

        imported = None
        if account is not None:
            canonical = AlpacaProviderAdapter().canonical_account_id(account)
            # The ceremony registers its synthetic session first — a
            # confirmation is only ever fenced by a real registration's
            # instance and epoch, even a migration's.
            from app.broker.fleet.provider import FLEET_PROTOCOL_VERSION

            migration_session = service.register_agent_session(
                clerk_id=clerk_id,
                worker_key=clerk_row.worker_key,
                agent_instance_id=_MIGRATION_INSTANCE,
                fleet_protocol_version=FLEET_PROTOCOL_VERSION,
            )
            service.reserve_assignment(
                broker=clerk_row.broker, clerk_id=clerk_id, external_account_id=account
            )
            imported = service.confirm_assignment(
                broker=clerk_row.broker,
                clerk_id=clerk_id,
                external_account_id=account,
                binding_generation=generation,
                agent_instance_id=_MIGRATION_INSTANCE,
                routing_epoch=migration_session.routing_epoch,
                effective_profile_id=profile_id,
                effective_revision=revision,
            )
            evidence = read_confirmation_evidence(volume_root)
            if evidence is None or evidence.binding_generation != generation:
                write_confirmation_evidence(
                    volume_root,
                    ConfirmationEvidence(
                        clerk_id=clerk_id,
                        volume_id=clerk_row.volume_id,
                        registry_id=service._store.registry_id,
                        assignment_generation=imported.assignment_generation,
                        canonical_account_id=canonical,
                        binding_generation=generation,
                        effective_profile_id=profile_id,
                        effective_revision=revision,
                        confirmed_at_ms=now_ms_utc(),
                        agent_instance_id=_MIGRATION_INSTANCE,
                        routing_epoch=migration_session.routing_epoch,
                    ),
                )

        _write(
            {
                "step": "complete",
                "clerk_id": clerk_id,
                "broker": clerk_row.broker,
                "volume_id": clerk_row.volume_id,
                "deployment_namespace": clerk_row.deployment_namespace,
                "binding_generation_seeded": seeded,
                "binding_generation": generation,
                "assignment": (
                    None
                    if imported is None
                    else {
                        "canonical_account_id": imported.canonical_external_account_id,
                        "assignment_generation": imported.assignment_generation,
                        "state": str(imported.state),
                    }
                ),
                "next": "approve-endpoint, then start the agent with FLEET_CLERK_ID, "
                "FLEET_WORKER_KEY and the agent service token.",
            }
        )
    finally:
        service.close()
    return 0


#: The migration's synthetic confirming session: a reserved instance identity
#: no live registration can present, so migrated confirmations are auditable
#: as ceremony writes and every real agent re-confirms under its own session
#: on first boot.
_MIGRATION_INSTANCE = "agnt_migrationceremony000000"
_MIGRATION_EPOCH = 1


def _effective_tuple(volume_root: Path) -> tuple[str | None, str | None, int | None]:
    """Read the existing effective tuple from the volume's profiles database."""
    from app.broker_configuration.store import ProfilesStore, profiles_database_path

    if not profiles_database_path(volume_root).exists():
        return None, None, None
    store = ProfilesStore.open(clerk_dir=volume_root)
    try:
        selection = store.read_selection()
    finally:
        store.close()
    return (
        selection.effective_account_id,
        selection.effective_profile_id,
        selection.effective_revision,
    )


def _seed_effective_binding_generation(volume_root: Path) -> tuple[bool, int]:
    """Seed generation 1 for an existing effective tuple, once, under the lock.

    Returns whether this run performed the seed and the effective generation
    the volume now carries. A volume that already advanced past 1 through
    Apply cycles keeps its generation, and the imported confirmation cites
    the volume's value — a hardcoded 1 would leave the registry's confirmed
    observation disagreeing with the clerk's own selection until the next
    re-confirmation.
    """
    from app.broker_configuration.runtime import build_service
    from app.broker_configuration.store import ProfilesStore, profiles_database_path
    from app.broker_configuration.worker_lifecycle import selection_handover

    if not profiles_database_path(volume_root).exists():
        return False, 0
    with selection_handover(service_factory=lambda: build_service(clerk_dir=volume_root)):
        store = ProfilesStore.open(clerk_dir=volume_root)
        try:
            with store.transaction() as conn:
                cursor = conn.execute(
                    "UPDATE installation_selection SET effective_binding_generation = 1 "
                    "WHERE id = 1 AND effective_binding_generation = 0 "
                    "AND effective_profile_id IS NOT NULL"
                )
                seeded = cursor.rowcount == 1
                row = conn.execute(
                    "SELECT effective_binding_generation FROM installation_selection "
                    "WHERE id = 1"
                ).fetchone()
                generation = int(row[0]) if row is not None else 0
                return seeded, generation
        finally:
            store.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage_broker_fleet",
        description="Host ceremonies for the broker clerk fleet control plane.",
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)

    def _with_control(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--control-dir", required=True, help="Coordinator control volume root")

    init = subparsers.add_parser("init", help="Create or open the fleet registry")
    _with_control(init)
    init.set_defaults(func=_init)

    provision = subparsers.add_parser(
        "provision", help="Provision one clerk lane on a fresh named volume"
    )
    _with_control(provision)
    provision.add_argument("--broker", required=True)
    provision.add_argument("--label", required=True)
    provision.add_argument("--volume-root", required=True, help="Mounted volume root for the clerk")
    provision.add_argument("--attestation-id", default=None, help="Nonsecret mount attestation")
    provision.add_argument(
        "--deployment-namespace",
        default="host:local",
        help="Deployment namespace qualifying volume roots (for example compose:prod)",
    )
    provision.set_defaults(func=_provision)

    migrate = subparsers.add_parser(
        "migrate-existing",
        help="Enrol the existing Alpaca lane's volume into the fleet (offline, resumable)",
    )
    _with_control(migrate)
    migrate.add_argument("--broker", default="alpaca")
    migrate.add_argument("--label", default="Migrated Alpaca lane")
    migrate.add_argument("--volume-root", required=True)
    migrate.add_argument("--attestation-id", default=None)
    migrate.add_argument("--deployment-namespace", default="host:local")
    migrate.set_defaults(func=_migrate_existing)

    rotate = subparsers.add_parser(
        "rotate-credentials",
        help="Mint one fresh environment-only transport token for a clerk",
    )
    _with_control(rotate)
    rotate.add_argument("--clerk-id", required=True)
    rotate.add_argument(
        "--slot",
        required=True,
        choices=("agent", "coordinator"),
        help="Which transport direction the token authenticates",
    )
    rotate.set_defaults(func=_rotate_credentials)

    approve = subparsers.add_parser(
        "approve-endpoint",
        help="Approve or re-target one clerk's internal agent destination",
    )
    _with_control(approve)
    approve.add_argument("--clerk-id", required=True)
    approve.add_argument(
        "--endpoint-ref", required=True, help="Stable reference registrations cite"
    )
    approve.add_argument(
        "--base-url",
        required=True,
        help="Internal destination, for example http://alpaca-paper-clerk:8000",
    )
    approve.set_defaults(func=_approve_endpoint)

    verify = subparsers.add_parser("verify", help="Re-run the volume identity gate")
    _with_control(verify)
    verify.add_argument("--clerk-id", required=True)
    verify.add_argument("--volume-root", required=True)
    verify.set_defaults(func=_verify)

    show = subparsers.add_parser("show", help="Print the fleet directory")
    _with_control(show)
    show.add_argument("--clerk-id", default=None)
    show.add_argument("--include-retired", action="store_true")
    show.set_defaults(func=_show)

    retire = subparsers.add_parser("retire", help="Retire a clerk (terminal)")
    _with_control(retire)
    retire.add_argument("--clerk-id", required=True)
    retire.set_defaults(func=_retire)

    release = subparsers.add_parser(
        "release-assignment", help="Host-only account release ceremony"
    )
    _with_control(release)
    release.add_argument("--broker", required=True)
    release.add_argument("--account-id", required=True)
    release.add_argument(
        "--expected-generation",
        type=int,
        required=True,
        help="The assignment generation this release's evidence was prepared against",
    )
    release.add_argument(
        "--proof",
        required=True,
        help="Offline-and-obligations-clear proof token",
    )
    release.set_defaults(func=_release)

    compatibility_snapshot = subparsers.add_parser(
        "compatibility-snapshot",
        help="Capture one privacy-preserving compatibility aggregate snapshot",
    )
    compatibility_snapshot.add_argument("--evidence-path", required=True)
    compatibility_snapshot.add_argument("--snapshot-path", required=True)
    compatibility_snapshot.add_argument(
        "--source-label",
        required=True,
        help="Short aggregate source name, such as combined, paper, or live",
    )
    compatibility_snapshot.set_defaults(func=_compatibility_snapshot)

    def _with_compatibility_evidence(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--start-snapshot", action="append", required=True)
        sp.add_argument("--end-snapshot", action="append", required=True)
        sp.add_argument("--consumer-inventory", required=True)
        sp.add_argument("--scoped-route-evidence", required=True)
        sp.add_argument("--operator-receipt", required=True)
        sp.add_argument(
            "--decision-receipt-path",
            required=True,
            help="Restricted-record path for the durable retirement decision",
        )
        sp.add_argument(
            "--max-evidence-age-ms",
            type=int,
            default=86_400_000,
            help="Maximum allowed age of end, inventory, health, and operator evidence",
        )
        sp.add_argument(
            "--max-window-duration-ms",
            type=int,
            default=604_800_000,
            help="Maximum bounded representative measurement-window duration",
        )

    compatibility_evaluate = subparsers.add_parser(
        "compatibility-evaluate",
        help="Evaluate evidence and leave compatibility reads in measurement mode",
    )
    _with_compatibility_evidence(compatibility_evaluate)
    compatibility_evaluate.set_defaults(func=_compatibility_evaluate)

    compatibility_retire = subparsers.add_parser(
        "compatibility-retire",
        help="Host-only retirement of eligible unscoped compatibility reads",
    )
    _with_compatibility_evidence(compatibility_retire)
    compatibility_retire.add_argument(
        "--route-state-path",
        action="append",
        required=True,
        help="Lane-local compatibility/route_state.json path; repeat for each serving lane",
    )
    compatibility_retire.set_defaults(func=_compatibility_retire)

    reassign = subparsers.add_parser(
        "reassign-assignment", help="Proof-driven host reassignment to an original successor volume"
    )
    _with_control(reassign)
    reassign.add_argument("--broker", required=True)
    reassign.add_argument("--account-id", required=True)
    reassign.add_argument("--expected-generation", type=int, required=True)
    reassign.add_argument("--proof", required=True)
    reassign.add_argument("--successor-clerk-id", required=True)
    reassign.add_argument("--successor-volume-root", required=True)
    reassign.set_defaults(func=_reassign)

    backup = subparsers.add_parser(
        "backup-registry", help="Create a nonsecret fleet-registry backup and manifest"
    )
    _with_control(backup)
    backup.add_argument("--backup-dir", required=True)
    backup.set_defaults(func=_backup_registry)

    restore = subparsers.add_parser(
        "restore-registry", help="Restore a registry backup with routing and assignments closed"
    )
    _with_control(restore)
    restore.add_argument("--backup-dir", required=True)
    restore.add_argument(
        "--d-compatible",
        action="store_true",
        help="Refuse anything newer than the Delivery-D registry schema",
    )
    restore.set_defaults(func=_restore_registry)

    rollback = subparsers.add_parser(
        "rollback-d-compatible",
        help="Restore a Delivery-D-compatible registry topology with recovery hold enabled",
    )
    _with_control(rollback)
    rollback.add_argument("--backup-dir", required=True)
    rollback.set_defaults(
        func=lambda args: _restore_registry(argparse.Namespace(**vars(args), d_compatible=True))
    )

    reconcile = subparsers.add_parser(
        "reconcile-registry", help="Reconcile one original Clerk volume after registry restore"
    )
    _with_control(reconcile)
    reconcile.add_argument("--clerk-id", required=True)
    reconcile.add_argument("--volume-root", required=True)
    reconcile.add_argument(
        "--provider-summary",
        required=True,
        help="Bounded provider summary JSON from the original lane's recovery observation",
    )
    reconcile.set_defaults(func=_reconcile_registry)

    closeout_empty = subparsers.add_parser(
        "closeout-empty-registry",
        help="Attest and close a restored registry with no effective assignments",
    )
    _with_control(closeout_empty)
    closeout_empty.add_argument("--operator", required=True)
    closeout_empty.add_argument(
        "--change-ref",
        required=True,
        help="Restricted incident or change record proving the host inventory closeout",
    )
    closeout_empty.set_defaults(func=_closeout_empty_registry)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch one CLI invocation; map refusals to the exit-code vocabulary."""
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (FleetControlError, CompatibilityRetirementRefusal) as exc:
        reason = exc.reason if isinstance(exc, FleetControlError) else "compatibility_retirement_refused"
        message = exc.message if isinstance(exc, FleetControlError) else str(exc)
        _write({"error": f"{reason}: {message}"})
        return 2
    except (argparse.ArgumentError, OSError, ValueError) as exc:
        _write({"error": str(exc)})
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FleetRegistryUnavailable as exc:  # pragma: no cover - direct-invocation path
        sys.stderr.write(json.dumps({"error": f"{exc.reason}: {exc.message}"}) + "\n")
        raise SystemExit(2)
