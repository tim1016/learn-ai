"""Run Delivery E's bounded multi-provider conformance ceremony.

This source-checkout command runs a focused fake-provider regression selection
and writes a machine-readable evidence bundle.  Its result is code evidence
only: it deliberately leaves isolated qualification, operator sign-off, and
production rollout as required records.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from scripts.broker_fleet_conformance import (
    DEFAULT_TIMEOUT_S,
    FleetConformanceError,
    build_evidence_bundle,
    run_targeted_code_checks,
    verify_fake_provider_production_boundary,
    write_evidence_bundle,
)

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the deliberately small bounded-host ceremony surface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-path",
        type=Path,
        required=True,
        help="Nonsecret, access-controlled JSON path outside the source checkout.",
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help="Focused pytest bound in seconds (1 through 120; default: 90).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the focused checks and emit only a code-conformance record."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        code_checks = run_targeted_code_checks(
            python_executable=sys.executable,
            timeout_s=args.timeout_s,
        )
        fake_boundary = verify_fake_provider_production_boundary()
        bundle = build_evidence_bundle(
            code_checks=code_checks,
            fake_provider_boundary=fake_boundary,
        )
        write_evidence_bundle(args.evidence_path, bundle)
    except (FleetConformanceError, OSError) as exc:
        logger.error("Fleet provider conformance ceremony failed.", extra={"error": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
