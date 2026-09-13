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
        --control-dir /app/artifacts/fleet --clerk-id clrk_...

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

from app.broker.fleet.errors import FleetControlError, FleetRegistryUnavailable
from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from scripts._operator_cli import jsonable


def _write(payload: object) -> None:
    print(json.dumps(jsonable(payload), sort_keys=True))


def _service(args: argparse.Namespace) -> FleetControlService:
    store = FleetRegistryStore.open(control_dir=Path(args.control_dir))
    return FleetControlService(store=store)


def _init(args: argparse.Namespace) -> int:
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
    service = _service(args)
    try:
        provisioned = service.provision_clerk(
            broker=args.broker,
            display_label=args.label,
            volume_root=Path(args.volume_root),
            attestation_id=args.attestation_id,
        )
        _write(
            {
                "clerk_id": provisioned.clerk.clerk_id,
                "broker": provisioned.clerk.broker,
                "volume_id": provisioned.clerk.volume_id,
                "worker_key": provisioned.clerk.worker_key,
                "attestation": (
                    provisioned.clerk.volume_attestation_kind,
                    provisioned.clerk.volume_attestation_id,
                ),
                "note": "Hand the worker key to exactly one agent process; it "
                "never crosses an API.",
            }
        )
    finally:
        service.close()
    return 0


def _verify(args: argparse.Namespace) -> int:
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
    service = _service(args)
    try:
        retired = service.retire_clerk(clerk_id=args.clerk_id)
        _write({"clerk_id": retired.clerk_id, "lifecycle_state": str(retired.lifecycle_state)})
    finally:
        service.close()
    return 0


def _release(args: argparse.Namespace) -> int:
    service = _service(args)
    try:
        released = service.release_assignment(
            broker=args.broker,
            external_account_id=args.account_id,
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
    provision.set_defaults(func=_provision)

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
        "--proof",
        required=True,
        help="Offline-and-obligations-clear proof token",
    )
    release.set_defaults(func=_release)
    return parser


def main(argv: list[str] | None = None) -> int:
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
        print(json.dumps({"error": f"{exc.reason}: {exc.message}"}), file=sys.stderr)
        raise SystemExit(2)
