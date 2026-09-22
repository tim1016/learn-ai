"""The fleet CLI's lifecycle ceremonies: handler and parser co-located.

``manage_broker_fleet`` grew past a readable single file when ADR 0063's
drain ceremony added its verbs, so each ceremony now lives as one unit —
the handler beside the subparser registration that invokes it — instead of
being split across the handler table and the parser table of the main
module. The main module keeps the shared plumbing (``_write``, ``_service``,
the exit-code mapping) and passes it in; this module owns the drain,
lane-quiet, retire, force-retire, release-assignment and reassign-assignment
surfaces and nothing else.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

from app.broker.fleet.service import FleetControlService


def register_lifecycle_ceremony_subparsers(
    subparsers: argparse._SubParsersAction,
    *,
    service_factory: Callable[[argparse.Namespace], FleetControlService],
    write: Callable[[object], None],
) -> None:
    """Add the lifecycle ceremonies, each handler beside its parser setup."""

    def _with_control(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--control-dir", required=True, help="Coordinator control volume root")

    def _drain(args: argparse.Namespace) -> int:
        """Handle ``drain``: close a provisioned lane's door (ADR 0063 Decision 1)."""
        service = service_factory(args)
        try:
            drained = service.drain_clerk(clerk_id=args.clerk_id)
            write(
                {
                    "clerk_id": drained.clerk_id,
                    "lifecycle_state": str(drained.lifecycle_state),
                    "draining_since_ms": drained.draining_since_ms,
                    "drain_deadline_at_ms": drained.drain_deadline_at_ms,
                }
            )
        finally:
            service.close()
        return 0

    drain = subparsers.add_parser(
        "drain", help="Close a provisioned lane's door (terminal-ward, irreversible)"
    )
    _with_control(drain)
    drain.add_argument("--clerk-id", required=True)
    drain.set_defaults(func=_drain)

    def _lane_quiet(args: argparse.Namespace) -> int:
        """Handle ``lane-quiet``: would retire or a release accept this lane's proof?"""
        service = service_factory(args)
        try:
            confirmation = service.check_lane_quiet(clerk_id=args.clerk_id)
            write(
                {
                    "clerk_id": confirmation.clerk_id,
                    "quiet": confirmation.is_quiet,
                    "observed_at_ms": confirmation.observed_at_ms,
                }
            )
        finally:
            service.close()
        return 0

    lane_quiet = subparsers.add_parser(
        "lane-quiet",
        help="Check, read-only, whether a draining lane's lane-quiet proof is fresh and quiet",
    )
    _with_control(lane_quiet)
    lane_quiet.add_argument("--clerk-id", required=True)
    lane_quiet.set_defaults(func=_lane_quiet)

    def _retire(args: argparse.Namespace) -> int:
        """Handle ``retire``: retire a clerk terminally."""
        service = service_factory(args)
        try:
            retired = service.retire_clerk(clerk_id=args.clerk_id)
            write(
                {"clerk_id": retired.clerk_id, "lifecycle_state": str(retired.lifecycle_state)}
            )
        finally:
            service.close()
        return 0

    retire = subparsers.add_parser("retire", help="Retire a clerk (terminal)")
    _with_control(retire)
    retire.add_argument("--clerk-id", required=True)
    retire.set_defaults(func=_retire)

    def _force_retire(args: argparse.Namespace) -> int:
        """Handle ``force-retire``: the named exit for a drain that cannot answer."""
        service = service_factory(args)
        try:
            retired = service.force_retire_clerk(
                clerk_id=args.clerk_id,
                operator=args.operator,
                change_ref=args.change_ref,
            )
            write(
                {
                    "clerk_id": retired.clerk_id,
                    "lifecycle_state": str(retired.lifecycle_state),
                    "lane_confirmation": (
                        None
                        if retired.lane_confirmation is None
                        else str(retired.lane_confirmation)
                    ),
                    "retire_operator": retired.retire_operator,
                    "retired_at_ms": retired.retired_at_ms,
                }
            )
        finally:
            service.close()
        return 0

    force_retire = subparsers.add_parser(
        "force-retire",
        help="The named exit for a drain whose lane cannot answer lane quiet",
    )
    _with_control(force_retire)
    force_retire.add_argument("--clerk-id", required=True)
    force_retire.add_argument(
        "--operator",
        required=True,
        help="Who is performing the force-retirement (bounded, attributed)",
    )
    force_retire.add_argument(
        "--change-ref",
        required=True,
        help="Incident or change record naming why (bounded, attributed)",
    )
    force_retire.set_defaults(func=_force_retire)

    def _release(args: argparse.Namespace) -> int:
        """Handle ``release-assignment``: run the host release ceremony."""
        service = service_factory(args)
        try:
            released = service.release_assignment(
                broker=args.broker,
                external_account_id=args.account_id,
                expected_assignment_generation=args.expected_generation,
                operator=args.operator,
                change_ref=args.change_ref,
            )
            write(
                {
                    "broker": released.broker,
                    "canonical_external_account_id": released.canonical_external_account_id,
                    "state": str(released.state),
                    "assignment_generation": released.assignment_generation,
                    "lane_confirmation": (
                        None
                        if released.lane_confirmation is None
                        else str(released.lane_confirmation)
                    ),
                    "attested_operator": released.attested_operator,
                }
            )
        finally:
            service.close()
        return 0

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
        "--operator",
        required=True,
        help="Who is releasing (bounded, attributed; replaces the old proof token)",
    )
    release.add_argument(
        "--change-ref",
        required=True,
        help="Incident or change record naming why (bounded, attributed)",
    )
    release.set_defaults(func=_release)

    def _reassign(args: argparse.Namespace) -> int:
        """Handle ``reassign-assignment``: move one account to a successor lane.

        Refuses (exit 2) unless the drained predecessor's current session holds
        a fresh, quiet lane-quiet confirmation (#2154)."""
        service = service_factory(args)
        try:
            successor = service.reassign_assignment(
                broker=args.broker,
                external_account_id=args.account_id,
                expected_assignment_generation=args.expected_generation,
                operator=args.operator,
                change_ref=args.change_ref,
                successor_clerk_id=args.successor_clerk_id,
                successor_volume_root=Path(args.successor_volume_root),
            )
            write(
                {
                    "broker": successor.broker,
                    "canonical_external_account_id": successor.canonical_external_account_id,
                    "clerk_id": successor.clerk_id,
                    "state": str(successor.state),
                    "assignment_generation": successor.assignment_generation,
                }
            )
        finally:
            service.close()
        return 0

    reassign = subparsers.add_parser(
        "reassign-assignment",
        help="Attributed host reassignment to an original successor volume "
        "(requires the drained lane's lane-quiet confirmation)",
    )
    _with_control(reassign)
    reassign.add_argument("--broker", required=True)
    reassign.add_argument("--account-id", required=True)
    reassign.add_argument("--expected-generation", type=int, required=True)
    reassign.add_argument(
        "--operator",
        required=True,
        help="Who is reassigning (bounded, attributed; replaces the old proof token)",
    )
    reassign.add_argument(
        "--change-ref",
        required=True,
        help="Incident or change record naming why (bounded, attributed)",
    )
    reassign.add_argument("--successor-clerk-id", required=True)
    reassign.add_argument("--successor-volume-root", required=True)
    reassign.set_defaults(func=_reassign)


__all__ = ["register_lifecycle_ceremony_subparsers"]
