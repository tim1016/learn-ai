"""Run and archive the deterministic Account Custody S15 qualification.

The command intentionally has no broker client and never upgrades a
deterministic result into a paper qualification. A trusted paper-run verifier
is a separate future integration.

Usage::

    python -m scripts.run_account_custody_qualification \
      --account-id DU1234567 \
      --output docs/audits/account-custody-qualification-DU1234567.json

"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from app.schemas.artifact_io import atomic_write_pydantic_artifact
from app.services.account_custody_qualification import run_deterministic_account_custody_qualification


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts.run_account_custody_qualification",
        description="Run the broker-free eight-bot Account Custody qualification.",
    )
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--generated-at-ms",
        type=int,
        help="Pinned UTC epoch milliseconds for a reproducible report; defaults to the current UTC epoch.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the deterministic campaign and emit one content-addressed report."""

    args = _parse_args(argv)
    generated_at_ms = args.generated_at_ms if args.generated_at_ms is not None else time.time_ns() // 1_000_000
    report = run_deterministic_account_custody_qualification(
        account_id=args.account_id,
        generated_at_ms=generated_at_ms,
    )
    payload = report.model_dump(mode="json")
    atomic_write_pydantic_artifact(args.output, report)
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
