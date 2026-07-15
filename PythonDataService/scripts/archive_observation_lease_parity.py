"""Archive durable observation-lease shadow-parity evidence for #1021.

The command never changes lease enforcement.  It snapshots the canonical
account-events journal, replays the promotion gate, and writes an auditable
JSON report that pins the exact input bytes by SHA-256.

Usage::

    python -m scripts.archive_observation_lease_parity \
      --artifacts-root PythonDataService/artifacts \
      --account-id DUM284968 \
      --output docs/audits/observation-lease-parity-DUM284968.json \
      --require-ready
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from app.services.observation_lease_parity import observation_lease_shadow_parity_archive_payload


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts.archive_observation_lease_parity",
        description="Archive Account Observation Lease cutover parity evidence.",
    )
    parser.add_argument("--artifacts-root", type=Path, required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="Return exit code 2 unless the archived evidence satisfies the cutover gate.",
    )
    return parser.parse_args(argv)


def _write_json_atomically(path: Path, payload: dict[str, object]) -> None:
    """Persist a complete report or leave the preceding archive untouched."""

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary.write(text)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def main(argv: list[str] | None = None) -> int:
    """Replay and archive one account's cutover evidence."""

    args = _parse_args(argv)
    payload = observation_lease_shadow_parity_archive_payload(
        args.artifacts_root,
        args.account_id,
    )
    _write_json_atomically(args.output, payload)
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if payload["cutover_ready"] or not args.require_ready else 2


if __name__ == "__main__":
    sys.exit(main())
