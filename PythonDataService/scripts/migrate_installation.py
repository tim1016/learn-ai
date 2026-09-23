"""Move the whole installation between two hosts, flat accounts only (#2268).

A host CLI, run from the repo's ``PythonDataService`` directory with the
host venv — not inside the data-plane image — because it needs what only the
host has: ``podman``, the host-folder binds, and the checkout's git history.
That is why it is its own script rather than more ``manage_broker_fleet``
subcommands, which run inside the image against the control volume. It keeps
that tool's conventions: argparse subcommands, one JSON line per step on
stdout, and the same exit codes.

Old machine::

    python -m scripts.migrate_installation export --check
    python -m scripts.migrate_installation export \\
        --bundle ~/learn-ai-2026-09-22.tar \\
        --operator inkant --change-ref migrate-2026-09-22

``export --check`` asks every lane whether its account is flat and stops
nothing and writes nothing. ``export`` refuses unless every account is flat
— before it stops anything — then stops every bot on every lane (they stay
stopped), re-checks, stops the containers that write the bundled data, and
writes one bundle file. It never drains a lane, never flattens anything,
cancels nothing but the stopped bots' own entry orders, and does not lock
the machine.

New machine, after copying the bundle and ``deploy/fleet/env/*.env`` (and
the repo-root ``.env`` and ``PythonDataService/.env``) by hand::

    python -m scripts.migrate_installation import --bundle ~/learn-ai-2026-09-22.tar

``import`` verifies everything before it changes anything, moves existing
data aside (never deletes it), restores, re-verifies, and leaves the stack
stopped with every lane **held for go-live**: no bot can start. Then bring
the stack up and::

    python -m scripts.migrate_installation go-live \
        --operator inkant --change-ref migrate-2026-09-22

``go-live`` proves IB Gateway returns historical bars to every lane, then
asks you to type that the old machine is off, then releases every lane. It
starts no bot. The procedure is ``docs/runbooks/migrate-installation.md``.

The coordinator is reached at ``--coordinator-url`` (default
``http://127.0.0.1:8000``) with ``DATA_PLANE_CONTROL_SECRET`` from the
environment or, failing that, the repo-root ``.env``.

Exit codes: ``0`` the command answered; ``1`` it could not be run as asked;
``2`` the migration refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from app.installation_migration.errors import MigrationRefused
from app.installation_migration.export import ExportRequest, run_export
from app.installation_migration.git import GitPort, SubprocessGit
from app.installation_migration.golive import GoLiveRequest, run_go_live
from app.installation_migration.importer import ImportRequest, run_import
from app.installation_migration.lanes import (
    DEFAULT_COORDINATOR_URL,
    CoordinatorLanes,
    FleetLanes,
    GoLiveLanes,
)
from app.installation_migration.podman import PodmanPort, SubprocessPodman
from app.installation_migration.topology import compose_variable
from scripts._operator_cli import jsonable

REPO_ROOT = Path(__file__).resolve().parents[2]
_CONTROL_SECRET_ENV = "DATA_PLANE_CONTROL_SECRET"


def _write(payload: object) -> None:
    """Emit one machine-readable JSON line to stdout (never ``print``)."""
    sys.stdout.write(json.dumps(jsonable(payload), sort_keys=True) + "\n")
    sys.stdout.flush()


def _control_secret(repo_root: Path) -> str | None:
    """The coordinator's control secret, as Compose hands it to the coordinator."""
    return compose_variable(repo_root, _CONTROL_SECRET_ENV)


@dataclass
class Ports:
    """The outside worlds the migration talks to."""

    podman: PodmanPort
    git: GitPort
    lanes: FleetLanes | None
    go_live_lanes: GoLiveLanes | None = None


def build_ports(args: argparse.Namespace) -> Ports:
    """The real adapters; tests replace this function."""
    repo_root = Path(args.repo_root)
    coordinator = (
        CoordinatorLanes(base_url=args.coordinator_url, control_secret=_control_secret(repo_root))
        if args.operation in ("export", "go-live")
        else None
    )
    return Ports(
        podman=SubprocessPodman(),
        git=SubprocessGit(repo_root),
        lanes=coordinator if args.operation == "export" else None,
        go_live_lanes=coordinator if args.operation == "go-live" else None,
    )


def _export(args: argparse.Namespace, ports: Ports) -> int:
    if not args.check and (not args.bundle or not args.operator or not args.change_ref):
        raise ValueError("export needs --bundle, --operator and --change-ref (or --check)")
    if ports.lanes is None:
        raise ValueError("export needs the coordinator's lane surface")
    run_export(
        ExportRequest(
            repo_root=Path(args.repo_root),
            bundle_path=Path(args.bundle).expanduser().resolve() if args.bundle else None,
            operator=args.operator or "",
            change_ref=args.change_ref or "",
            check_only=args.check,
            lake_dir=Path(args.lake_dir) if args.lake_dir else None,
            allow_dirty_tree=args.allow_dirty_tree,
        ),
        lanes=ports.lanes,
        podman=ports.podman,
        git=ports.git,
        emit=_write,
    )
    return 0


def _import(args: argparse.Namespace, ports: Ports) -> int:
    run_import(
        ImportRequest(
            repo_root=Path(args.repo_root),
            bundle_path=Path(args.bundle).expanduser().resolve(),
            aside_dir=Path(args.aside_dir).expanduser().resolve() if args.aside_dir else None,
            lake_dir=Path(args.lake_dir) if args.lake_dir else None,
            accept_dirty_source=args.accept_dirty_source,
        ),
        podman=ports.podman,
        git=ports.git,
        emit=_write,
    )
    return 0


def _read_confirmation(prompt: str) -> str:
    """Prompt on stderr (stdout is the JSON-line channel); read one typed line."""
    sys.stderr.write(prompt)
    sys.stderr.flush()
    return sys.stdin.readline().rstrip("\r\n")


def _go_live(args: argparse.Namespace, ports: Ports) -> int:
    if ports.go_live_lanes is None:
        raise ValueError("go-live needs the coordinator's lane surface")
    run_go_live(
        GoLiveRequest(operator=args.operator, change_ref=args.change_ref),
        lanes=ports.go_live_lanes,
        confirm=_read_confirmation,
        emit=_write,
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_installation",
        description="Move the whole installation between two hosts (flat accounts only).",
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)

    def _common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--repo-root", default=str(REPO_ROOT), help="The checkout to act for")
        sp.add_argument(
            "--lake-dir",
            default=None,
            help="Host lake folder (default: LEAN_DATA_VOLUME_HOST_PATH, else ./data-lake-volume)",
        )

    export = subparsers.add_parser(
        "export", help="Require every account flat, stop every bot, write one bundle"
    )
    _common(export)
    export.add_argument("--bundle", default=None, help="Bundle file to write (never overwritten)")
    export.add_argument("--operator", default=None, help="Who is migrating (recorded)")
    export.add_argument("--change-ref", default=None, help="Change record (recorded)")
    export.add_argument(
        "--check",
        action="store_true",
        help="Only ask every lane whether its account is flat; stop and write nothing",
    )
    export.add_argument(
        "--allow-dirty-tree",
        action="store_true",
        help="Export although tracked files have uncommitted changes (recorded in the manifest)",
    )
    export.add_argument("--coordinator-url", default=DEFAULT_COORDINATOR_URL)
    export.set_defaults(func=_export)

    restore = subparsers.add_parser(
        "import", help="Verify a bundle, move existing data aside, restore, re-verify"
    )
    _common(restore)
    restore.add_argument("--bundle", required=True, help="Bundle file to restore")
    restore.add_argument(
        "--aside-dir",
        default=None,
        help="Where existing data is moved (default: <checkout>-migration-aside beside it)",
    )
    restore.add_argument(
        "--accept-dirty-source",
        action="store_true",
        help="Restore a bundle exported from a checkout with uncommitted changes",
    )
    restore.set_defaults(func=_import)

    go_live = subparsers.add_parser(
        "go-live",
        help="Prove IB Gateway bars on every lane, confirm the old machine is off, "
        "release every lane (starts no bot)",
    )
    go_live.add_argument("--repo-root", default=str(REPO_ROOT), help="The checkout to act for")
    go_live.add_argument("--operator", required=True, help="Who is taking the lanes live")
    go_live.add_argument("--change-ref", required=True, help="Change record (recorded)")
    go_live.add_argument("--coordinator-url", default=DEFAULT_COORDINATOR_URL)
    go_live.set_defaults(func=_go_live)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch one invocation; map refusals to the exit-code vocabulary."""
    args = _build_parser().parse_args(argv)
    ports: Ports | None = None
    try:
        ports = build_ports(args)
        return args.func(args, ports)
    except MigrationRefused as exc:
        _write(exc.to_json())
        return 2
    except (OSError, ValueError) as exc:
        _write({"error": str(exc)})
        return 1
    finally:
        if ports is not None:
            for surface in (ports.lanes, ports.go_live_lanes):
                if isinstance(surface, CoordinatorLanes):
                    surface.close()


if __name__ == "__main__":
    raise SystemExit(main())
