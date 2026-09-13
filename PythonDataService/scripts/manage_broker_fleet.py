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

from app.broker.fleet.errors import (
    ClerkVolumeCloneDetected,
    FleetControlError,
    FleetRegistryUnavailable,
)
from app.broker.fleet.identity import new_service_token
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
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch one CLI invocation; map refusals to the exit-code vocabulary."""
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FleetControlError as exc:
        _write({"error": f"{exc.reason}: {exc.message}"})
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
