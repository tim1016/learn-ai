"""Emit ADR 0037's all-or-nothing activation-inventory qualification receipt."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from app.broker.alpaca.clerk.sqlite.activation_inventory import (
    qualify_activation_inventory,
    read_activation_inventory,
)
from app.broker.alpaca.clerk.sqlite.operational_files import atomic_write_json
from app.utils.timestamps import now_ms_utc


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts.qualify_alpaca_activation_inventory",
        description=(
            "Verify that every account in a fresh operator inventory has a valid, "
            "database-bound Alpaca SQLite activation fence."
        ),
    )
    parser.add_argument("--artifacts-root", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--max-inventory-age-ms", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    receipt = qualify_activation_inventory(
        read_activation_inventory(args.inventory),
        artifacts_root=args.artifacts_root,
        qualified_at_ms=now_ms_utc(),
        max_inventory_age_ms=args.max_inventory_age_ms,
    )
    payload = asdict(receipt)
    atomic_write_json(args.output, payload)
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
