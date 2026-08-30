"""Administrative command for a physical lake root's identity marker (#1876).

app.data_lake.root_identity deliberately exposes no way for normal
application code to create ``.data-root.json`` — only this command (and the
functions it wraps) ever writes one. Three subcommands:

    python -m scripts.manage_data_root init   --root-id <uuid> [--base-root PATH]
    python -m scripts.manage_data_root stamp  --root-id <uuid> --force [--base-root PATH]
    python -m scripts.manage_data_root inspect [--base-root PATH]

``init`` claims a brand-new, empty root. ``stamp`` assigns an identity to an
existing (possibly populated) canonical root during rollout — it requires
``--force`` as the operator's explicit acknowledgment that it is claiming
whatever is already on disk. ``inspect`` reads a root's marker (or reports
that none exists) without changing anything. ``--base-root`` defaults to the
configured ``LEAN_DATA_WRITE_ROOT``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from uuid import UUID

from app.config import settings
from app.data_lake.root_identity import (
    LakeRootIdentityError,
    inspect_root,
    init_empty_root,
    marker_path,
    stamp_existing_root,
)

logger = logging.getLogger("manage_data_root")


def run_init(base_root: Path, root_id: UUID) -> None:
    init_empty_root(base_root, root_id)
    logger.info("initialized %s with data_root_id=%s", marker_path(base_root), root_id)


def run_stamp(base_root: Path, root_id: UUID, *, force: bool) -> None:
    stamp_existing_root(base_root, root_id, force=force)
    logger.info("stamped %s with data_root_id=%s", marker_path(base_root), root_id)


def run_inspect(base_root: Path) -> None:
    marker = inspect_root(base_root)
    if marker is None:
        logger.info("no root-identity marker at %s", marker_path(base_root))
        return
    logger.info(
        "%s: schema_version=%s data_root_id=%s", marker_path(base_root), marker.schema_version, marker.data_root_id
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_base_root_arg(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--base-root",
            type=Path,
            default=Path(settings.LEAN_DATA_WRITE_ROOT),
            help="the write root holding lake/ (default: LEAN_DATA_WRITE_ROOT)",
        )

    init_parser = subparsers.add_parser("init", help="claim a brand-new, empty root")
    add_base_root_arg(init_parser)
    init_parser.add_argument("--root-id", type=UUID, required=True)

    stamp_parser = subparsers.add_parser("stamp", help="assign an identity to an existing canonical root")
    add_base_root_arg(stamp_parser)
    stamp_parser.add_argument("--root-id", type=UUID, required=True)
    stamp_parser.add_argument(
        "--force", action="store_true", help="required: acknowledges this claims whatever is already on disk"
    )

    inspect_parser = subparsers.add_parser("inspect", help="read a root's marker without changing it")
    add_base_root_arg(inspect_parser)

    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            run_init(args.base_root, args.root_id)
        elif args.command == "stamp":
            run_stamp(args.base_root, args.root_id, force=args.force)
        elif args.command == "inspect":
            run_inspect(args.base_root)
    except LakeRootIdentityError as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
